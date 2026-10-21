"""Every number the owner sees agrees with the grouped seating (R5, R6).

The plan lens (the drawer's seated mix, the per-plan export, the filtered
panes) reads the evaluator's own seating, so with a mark it shows the grouped
sections. With the flag off a stored mark changes nothing at all: the
evaluation, the lens payload and the repair cache fingerprint are exactly what
they are with the mark cleared.
"""

from __future__ import annotations

from collections import Counter

import pytest
from django.test import override_settings

from core.models import TermSection
from core.services.timetable_plan_lens import build_scenario_plan_lens
from core.services.timetable_repair import _repair_state_fingerprint
from tests._section_programme_fixture import build_marked_scenario, evaluate
from tests.test_student_assignment_parity import _states_sig

pytestmark = pytest.mark.django_db

ON = override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=True)
OFF = override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=False)


def _evaluator_mix(scenario_id):
    result, profiles = evaluate(scenario_id)
    mix: dict[str, Counter] = {}
    for sid, state in result.assignment_states.items():
        for section_id in state.assigned_sections.values():
            mix.setdefault(section_id, Counter())[profiles[sid].department.upper()] += 1
    return mix, result


def _lens_mix(lens):
    return {
        f"{row['course_key']}_{row['section']}": Counter(
            {plan: n for plan, n in row["actual_plans"].items() if n}
        )
        for row in lens["sections"].values()
    }


@ON
def test_the_lens_shows_the_grouped_seating_the_evaluator_produces() -> None:
    scenario, _board, _placements = build_marked_scenario(mark="AI2")
    mix, result = _evaluator_mix(scenario.pk)
    lens = build_scenario_plan_lens(scenario.pk)
    assert _lens_mix(lens) == mix
    assert mix["AI236_S2"] == Counter({"AI2": 6})
    assert mix["AI236_S1"] == Counter({"DS": 6})
    grouping = lens["programme_grouping"]
    assert grouping == result.quality_score["programme_grouping"]
    assert grouping["marked_sections"] == ["AI236_S2"] and grouping["guard_reverted"] is False
    assert grouping["moves"] + grouping["swaps"] > 0, "the plain seating mixes the two"
    marks = {row["section"]: row["preferred_programme"] for row in lens["sections"].values()}
    assert marks == {"S1": "", "S2": "AI2"}


@ON
def test_with_the_flag_on_and_no_mark_the_lens_says_so() -> None:
    scenario, _board, _placements = build_marked_scenario(mark="")
    lens = build_scenario_plan_lens(scenario.pk)
    assert lens["programme_grouping"] is None
    assert {row["preferred_programme"] for row in lens["sections"].values()} == {""}


@OFF
def test_with_the_flag_off_a_stored_mark_changes_nothing() -> None:
    scenario, _board, _placements = build_marked_scenario(mark="AI2")
    marked_eval, _profiles = evaluate(scenario.pk)
    marked_lens = build_scenario_plan_lens(scenario.pk)
    marked_print = _repair_state_fingerprint(scenario.pk, [])

    TermSection.objects.filter(scenario=scenario).update(preferred_programme="")
    plain_eval, _profiles = evaluate(scenario.pk)
    plain_lens = build_scenario_plan_lens(scenario.pk)

    assert marked_eval.lexicographic_score == plain_eval.lexicographic_score
    assert _states_sig(marked_eval.assignment_states) == _states_sig(plain_eval.assignment_states)
    assert marked_eval.quality_score == plain_eval.quality_score
    assert "programme_grouping" not in marked_eval.quality_score
    assert marked_lens == plain_lens
    assert "programme_grouping" not in marked_lens
    assert all("preferred_programme" not in row for row in marked_lens["sections"].values())
    assert _repair_state_fingerprint(scenario.pk, []) == marked_print


def test_a_mark_change_invalidates_cached_repair_analyses_only_under_the_flag() -> None:
    scenario, _board, _placements = build_marked_scenario(mark="AI2")
    with OFF:
        off_before = _repair_state_fingerprint(scenario.pk, [])
    with ON:
        on_before = _repair_state_fingerprint(scenario.pk, [])
    TermSection.objects.filter(scenario=scenario, section="S2").update(preferred_programme="DS")
    with OFF:
        assert _repair_state_fingerprint(scenario.pk, []) == off_before
    with ON:
        assert _repair_state_fingerprint(scenario.pk, []) != on_before
        # The flag flip itself is a different seating too.
        assert on_before != off_before
