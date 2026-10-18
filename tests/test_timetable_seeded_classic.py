"""Seeded classic runs vary real choices while retaining quality and lock gates."""

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TimetableScenario,
)
from core.services.timetable_autoplace import auto_place_scenario
from core.services.timetable_board_persistence import reset_scenario
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_local_search_v2 import (
    diagnostic_driven_local_search,
    generate_all_repattern_moves,
)
from core.services.timetable_optimisation_policy import evaluation_is_better, schedule_metrics_of
from core.services.timetable_optimizer_v2 import _generate_candidates_for_scenario
from core.services.timetable_run_seed import seeded_timetable_run
from tests.test_timetable_full_locked_sections import partial_sections  # noqa: F401
from tests.test_timetable_search_regressions import _pattern, _profile, _section


def _symmetric_scenario():
    scenario = TimetableScenario.objects.create(
        name=f"Seed fixture {TimetableScenario.objects.count()}",
        academic_year="1448",
        term="1",
        gender="M",
        programs=["AI"],
        slot_config=[{"start": "09:00", "end": "10:15"}, {"start": "10:30", "end": "11:45"}],
    )
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1, program="AI")
    Room.objects.get_or_create(
        room_code="AI-SEED",
        defaults={"capacity": 50, "room_type": "lecture", "department": "AI", "section": "M"},
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="AI101",
        course_code="AI101",
        course_name="AI101",
        department="AI",
        credit_hours=3,
        planned_sections=1,
        max_per_section=30,
        total_demand=20,
        programme_term=1,
    )
    return scenario


def _layout(scenario):
    return tuple(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("term_section__course_key", "term_section__section", "day", "start_time")
        .values_list(
            "term_section__course_key", "term_section__section", "day", "start_time", "end_time"
        )
    )


def _state_layout(states):
    return tuple(
        sorted(
            (s.section_id, tuple(sorted((m.day, m.start_min, m.end_min) for m in s.meetings)))
            for s in states
        )
    )


@pytest.mark.django_db
def test_greedy_seed_replays_across_new_database_ids_and_varies_actual_layout():
    layouts = []
    for seed in range(12):
        scenario = _symmetric_scenario()
        with seeded_timetable_run(seed):
            auto_place_scenario(scenario.pk, strategy="compact")
        layout = _layout(scenario)
        assert len(layout) == 2
        assert len({row[2] for row in layout}) == 2
        layouts.append(layout)
    assert len(set(layouts)) > 1
    replay = _symmetric_scenario()
    with seeded_timetable_run(0):
        auto_place_scenario(replay.pk, strategy="compact")
    assert _layout(replay) == layouts[0]


@pytest.mark.django_db
def test_winning_strategy_replacement_reuses_candidate_stream_after_other_strategies():
    scenario = _symmetric_scenario()
    with seeded_timetable_run(814):
        candidates = _generate_candidates_for_scenario(
            scenario.pk, ["compact", "balanced", "morning"]
        )
        expected = _state_layout(candidates[0]["sections"])
        reset_scenario(scenario.pk, keep_locked=True)
        auto_place_scenario(scenario.pk, strategy="compact")
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    assert _state_layout(build_section_states_for_scenario(scenario.pk)) == expected


@pytest.mark.django_db
@pytest.mark.parametrize("seed", [0, 17])
def test_seeded_portfolio_retains_locked_complete_patterns_and_contact_coverage(request, seed):
    from core.services.timetable_optimizer_v2 import ALL_STRATEGIES
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    scenario, board = request.getfixturevalue("partial_sections")
    fixed_sections = list(
        SectionPlacement.objects.filter(board=board, is_locked=True).values_list(
            "term_section_id", flat=True
        )
    )
    fields = ("pk", "term_section_id", "day", "start_time", "end_time", "is_locked")
    before = list(
        SectionPlacement.objects.filter(term_section_id__in=fixed_sections)
        .order_by("pk")
        .values_list(*fields)
    )
    with seeded_timetable_run(seed):
        report = run_v2_optimisation_guarded(
            scenario.pk,
            mode="full",
            strategies=list(ALL_STRATEGIES),
            max_iterations=0,
            run_chain=False,
            run_cpsat=False,
        )
    assert not report.get("error")
    assert not report.get("safety_blocked")
    assert (
        list(
            SectionPlacement.objects.filter(term_section_id__in=fixed_sections)
            .order_by("pk")
            .values_list(*fields)
        )
        == before
    )
    assert SectionPlacement.objects.filter(board=board).count() == 7


def _current_fixture():
    fixed = _pattern("fixed", 540, "fixed")
    late = _pattern("late", 780)
    early = _pattern("before", 465)
    adjacent = _pattern("after", 615)
    sections = {"A": _section("A", late), "B": _section("B", fixed)}
    profiles = {"ab": _profile("ab", ["A", "B"])}
    catalog = {"normal": [late, early, adjacent], "fixed": [fixed]}
    teachers = {"A": frozenset({7}), "B": frozenset({7})}
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), profiles, {}, section_instructor_ids=teachers
    )
    return sections, profiles, catalog, teachers, baseline


def test_seeded_current_enumeration_can_choose_different_equal_best_improvements(settings):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_LOCAL_SEARCH_TIME_LIMIT_SECONDS = 10
    layouts = []
    move_orders = []
    for seed in range(8):
        sections, profiles, catalog, teachers, baseline = _current_fixture()
        fixed = list(sections["B"].meetings)
        with seeded_timetable_run(seed):
            move_orders.append(
                tuple(generate_all_repattern_moves(sections, catalog, locked_section_ids={"B"}))
            )
            result = diagnostic_driven_local_search(
                baseline,
                sections,
                catalog,
                profiles,
                {},
                locked_section_ids={"B"},
                section_instructor_ids=teachers,
                max_iterations=1,
            )
        assert sections["B"].meetings == fixed
        assert evaluation_is_better(result, baseline)
        assert schedule_metrics_of(result)["idle_minutes"] == 0
        assert schedule_metrics_of(result)["working_days"] == 1
        layouts.append(tuple((m.day, m.start_min, m.end_min) for m in sections["A"].meetings))
    assert len(set(move_orders)) > 1
    assert len(set(layouts)) == 2
    sections, profiles, catalog, teachers, baseline = _current_fixture()
    with seeded_timetable_run(0):
        replay = diagnostic_driven_local_search(
            baseline,
            sections,
            catalog,
            profiles,
            {},
            locked_section_ids={"B"},
            section_instructor_ids=teachers,
            max_iterations=1,
        )
    assert evaluation_is_better(replay, baseline)
    assert tuple((m.day, m.start_min, m.end_min) for m in sections["A"].meetings) == layouts[0]


def test_seeded_current_does_not_accept_equal_quality_layout_only_changes(settings):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    sections, profiles, catalog, teachers, baseline = _current_fixture()
    sections["A"].pattern_id = "after"
    sections["A"].meetings = list(catalog["normal"][2].meetings)
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), profiles, {}, section_instructor_ids=teachers
    )
    before = list(sections["A"].meetings)
    with seeded_timetable_run(4):
        result = diagnostic_driven_local_search(
            baseline,
            sections,
            catalog,
            profiles,
            {},
            locked_section_ids={"B"},
            section_instructor_ids=teachers,
            max_iterations=2,
        )
    assert sections["A"].meetings == before
    assert not evaluation_is_better(result, baseline)


def test_unseeded_current_retains_legacy_enumeration():
    sections, profiles, catalog, teachers, baseline = _current_fixture()
    moves = generate_all_repattern_moves(sections, catalog, locked_section_ids={"B"})
    assert [move.to_pattern_id_a for move in moves] == ["before", "after"]
    with seeded_timetable_run(None):
        assert generate_all_repattern_moves(sections, catalog, locked_section_ids={"B"}) == moves


@pytest.mark.django_db
@pytest.mark.parametrize("seed", [0, 3, 17])
def test_seeded_greedy_retains_complete_english_bundles(seed):
    from core.services.timetable_course_policy import get_course_meeting_policy
    from tests.test_timetable_classic_intensive_english import board_fixture

    scenario, board = board_fixture(count=2)
    with seeded_timetable_run(seed):
        auto_place_scenario(scenario.pk, strategy="compact")
    policy = get_course_meeting_policy("ENG101")
    section_ids = set(
        SectionPlacement.objects.filter(board=board).values_list("term_section_id", flat=True)
    )
    assert len(section_ids) == 2
    for section_id in section_ids:
        meetings = [
            {"day": day, "start": start, "end": end}
            for day, start, end in SectionPlacement.objects.filter(
                term_section_id=section_id
            ).values_list("day", "start_time", "end_time")
        ]
        assert len(meetings) == 4
        assert policy.accepts(meetings)
