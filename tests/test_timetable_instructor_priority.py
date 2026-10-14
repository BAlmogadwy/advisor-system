from __future__ import annotations

from dataclasses import replace
from itertools import permutations

import pytest
from django.test import override_settings

from core.services.timetable_assignment_models import (
    SectionMeeting,
    SectionState,
    TimetableEvaluationResult,
)
from core.services.timetable_candidate_eval import (
    evaluate_generated_timetable_candidate,
    rank_timetable_candidates,
)
from core.services.timetable_optimisation_policy import (
    evaluation_is_better,
    evaluation_order_key,
    instructor_idle_regressions,
    instructor_schedule_metrics,
    instructor_schedule_regressions,
)
from core.services.timetable_v2_runner import (
    optimiser_instructor_day_regression,
    optimiser_instructor_idle_regression,
)


def section(sid, day, start, end, *, online=False):
    return SectionState(sid, sid, [SectionMeeting(day, start, end)], 30, 0, is_online=online)


def evaluation(*, days=None, idle=0, idle_by=None, student_cost=0, hard=(0, 0, 0, 0)):
    days = {"1": 2} if days is None else days
    if idle_by is None:
        idle_by = dict.fromkeys(days, 0)
        if idle_by:
            idle_by[next(iter(idle_by))] = idle
    else:
        idle = sum(idle_by.values())
    return TimetableEvaluationResult(
        "test",
        (*hard, student_cost, 0, 0, 0, 0),
        {},
        [],
        [],
        [],
        [],
        quality_score={
            "instructor_schedule": {
                "working_days": sum(days.values()),
                "working_days_by_instructor": days,
                "idle_minutes": idle,
                "idle_minutes_by_instructor": idle_by,
                "affected_instructors": len(days),
            }
        },
    )


def test_metrics_union_nested_meetings_and_exclude_online():
    sections = [
        section("a", 0, 540, 720),
        section("b", 0, 600, 660),
        section("c", 0, 750, 810),
        section("d", 0, 720, 750, online=True),
        section("e", 1, 540, 600, online=True),
        section("f", 2, 540, 600),
    ]
    mapping = {s.section_id: frozenset({1, 2}) for s in sections}
    assert instructor_schedule_metrics(sections, mapping) == {
        "working_days": 4,
        "idle_minutes": 60,
        "working_days_by_instructor": {"1": 2, "2": 2},
        "affected_instructors": 2,
        "idle_minutes_by_instructor": {"1": 30, "2": 30},
    }


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_shorter_week_cannot_trade_worse_instructor_idle():
    before = evaluation(days={"1": 3}, idle=0)
    after = evaluation(days={"1": 2}, idle=90, student_cost=600)
    assert not evaluation_is_better(after, before)
    assert after.lexicographic_score > before.lexicographic_score


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_instructor_gap_improvement_can_trade_student_gap():
    before = evaluation(idle=180)
    after = evaluation(idle=30, student_cost=600)
    assert evaluation_is_better(after, before)
    assert not evaluation_is_better(before, after)


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_total_idle_gain_cannot_hide_an_individual_gap_regression():
    before = evaluation(days={"1": 2, "2": 2}, idle_by={"1": 100, "2": 0})
    after = evaluation(days={"1": 1, "2": 2}, idle_by={"1": 0, "2": 30})
    assert evaluation_order_key(after) < evaluation_order_key(before)
    assert not evaluation_is_better(after, before)
    rows = instructor_idle_regressions(
        before.quality_score["instructor_schedule"], after.quality_score["instructor_schedule"]
    )
    assert [(r["instructor_id"], r["before"], r["after"]) for r in rows] == [("2", 0, 30)]


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_shorter_week_can_trade_student_quality_when_every_teacher_idle_is_protected():
    before = evaluation(days={"1": 3, "2": 2}, idle_by={"1": 90, "2": 30})
    after = evaluation(days={"1": 2, "2": 2}, idle_by={"1": 60, "2": 30}, student_cost=600)
    assert evaluation_is_better(after, before)


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_idle_alternatives_use_fixed_iteration_ceiling():
    current = evaluation(days={"1": 3, "2": 3}, idle_by={"1": 90, "2": 90})
    tentative = evaluation(days={"1": 2, "2": 3}, idle_by={"1": 0, "2": 90})
    challenger = evaluation(days={"1": 2, "2": 2}, idle_by={"1": 30, "2": 30})
    assert not evaluation_is_better(challenger, tentative)
    assert evaluation_is_better(
        challenger,
        tentative,
        day_ceilings=current.quality_score["instructor_schedule"],
        idle_ceilings=current.quality_score["instructor_schedule"],
        feasibility_ceilings=current,
    )


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("missing", ["map", "teacher", "null", "negative", "total", "count"])
@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_idle_protection_fails_closed_on_incomplete_evidence(side, missing):
    before = evaluation(days={"1": 2, "2": 2}, idle_by={"1": 90, "2": 30})
    after = evaluation(days={"1": 1, "2": 2}, idle_by={"1": 0, "2": 0})
    metrics = (before if side == "before" else after).quality_score["instructor_schedule"]
    if missing == "map":
        del metrics["idle_minutes_by_instructor"]
    elif missing == "teacher":
        del metrics["idle_minutes_by_instructor"]["2"]
    elif missing == "null":
        metrics["idle_minutes_by_instructor"]["2"] = None
    elif missing == "negative":
        metrics["idle_minutes_by_instructor"]["2"] = -1
    elif missing == "total":
        metrics["idle_minutes"] = None
    else:
        metrics["affected_instructors"] = 1
    assert instructor_idle_regressions(
        before.quality_score["instructor_schedule"], after.quality_score["instructor_schedule"]
    )
    assert not evaluation_is_better(after, before)


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=False)
def test_secondary_schedule_guard_is_independent_of_priority_kill_switch():
    before = evaluation(days={"1": 2}, idle_by={"1": 30}, student_cost=100)
    after = evaluation(days={"1": 3}, idle_by={"1": 60})
    assert evaluation_is_better(after, before)  # Legacy public score contract.
    metrics_before = before.quality_score["instructor_schedule"]
    metrics_after = after.quality_score["instructor_schedule"]
    rows = instructor_schedule_regressions(metrics_before, metrics_after)
    assert {r["metric"] for r in rows} == {
        "instructor_working_days",
        "instructor_total_working_days",
        "instructor_idle_minutes",
        "instructor_total_idle_minutes",
    }


def test_idle_guard_preserves_json_identity_and_requires_consistent_totals():
    before = evaluation(days={1: 2}, idle_by={1: 30}).quality_score["instructor_schedule"]
    after = evaluation(days={"1": 2}, idle_by={"1": 30}).quality_score["instructor_schedule"]
    assert not instructor_schedule_regressions(before, after)
    after["idle_minutes"] = 0
    assert instructor_idle_regressions(before, after)[0]["metric"].endswith("_evidence")


@pytest.mark.parametrize("index", range(4))
@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_each_hard_student_component_is_a_gate(index):
    before = evaluation(days={"1": 3}, hard=(2, 2, 2, 2))
    hard = [0, 0, 0, 0]
    hard[index] = 3
    after = evaluation(days={"1": 1}, hard=tuple(hard))
    assert not evaluation_is_better(after, before)


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_individual_ceiling_prevents_swapping_longer_weeks():
    before = evaluation(days={"1": 4, "2": 1})
    after = evaluation(days={"1": 2, "2": 2})
    assert evaluation_order_key(after) < evaluation_order_key(before)
    assert not evaluation_is_better(after, before)


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_move_alternatives_use_current_board_ceiling():
    current = evaluation(days={"1": 3, "2": 3}, idle=180)
    best = evaluation(days={"1": 2, "2": 3}, idle=90)
    challenger = evaluation(days={"1": 3, "2": 1}, idle=30)
    assert not evaluation_is_better(challenger, best)
    assert evaluation_is_better(
        challenger, best, day_ceilings=current.quality_score["instructor_schedule"]
    )


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_alternative_selection_is_independent_of_enumeration_order():
    current = evaluation(hard=(2, 2, 0, 0))
    lower_clashes = evaluation(hard=(2, 0, 0, 0))
    lower_high_risk = evaluation(hard=(1, 1, 0, 0))
    invalid_core = evaluation(hard=(0, 0, 1, 0))
    for alternatives in permutations((lower_clashes, lower_high_risk, invalid_core)):
        winner = current
        for candidate in alternatives:
            if evaluation_is_better(
                candidate,
                winner,
                feasibility_ceilings=current,
                day_ceilings=current.quality_score["instructor_schedule"],
            ):
                winner = candidate
        assert winner is lower_high_risk
    # A committed board still becomes the next iteration's hard ceiling.
    assert not evaluation_is_better(lower_high_risk, lower_clashes)


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_overload_ceiling_is_pinned_to_committed_board():
    current = evaluation(hard=(2, 2, 0, 0))
    current.instructor_overload_count = 2
    winner = evaluation(hard=(2, 0, 0, 0))
    challenger = evaluation(hard=(1, 1, 0, 0))
    challenger.instructor_overload_count = 1
    assert evaluation_is_better(challenger, winner, feasibility_ceilings=current)
    challenger.instructor_overload_count = 3
    assert not evaluation_is_better(challenger, winner, feasibility_ceilings=current)


@pytest.mark.parametrize("missing", ["all", "teacher", "idle", "affected"])
@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_missing_teacher_evidence_cannot_win_as_zero_days(missing):
    current = evaluation(days={"1": 3, "2": 2}, idle=90)
    candidate = evaluation(days={"1": 2, "2": 2}, idle=0)
    metrics = candidate.quality_score["instructor_schedule"]
    if missing == "all":
        candidate.quality_score.clear()
    elif missing == "teacher":
        del metrics["working_days_by_instructor"]["2"]
    elif missing == "idle":
        del metrics["idle_minutes"]
    else:
        metrics["affected_instructors"] = 0
    assert not evaluation_is_better(candidate, current)


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=False)
def test_kill_switch_restores_exact_student_lex_comparison():
    before = evaluation(days={"1": 3}, student_cost=100)
    after = evaluation(days={"1": 1}, student_cost=200)
    assert evaluation_order_key(after) == after.lexicographic_score
    assert not evaluation_is_better(after, before)
    assert evaluation_is_better(before, after)


@override_settings(
    TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True,
    TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=False,
    TIMETABLE_TIERED_OBJECTIVE_ENABLED=True,
)
def test_priority_works_with_old_gap_flag_off_and_preserves_tuple():
    sections = [section("a", 0, 540, 600), section("b", 0, 720, 780)]
    compact = [sections[0], replace(sections[1], meetings=[SectionMeeting(0, 600, 660)])]
    mapping = {"a": frozenset({1}), "b": frozenset({1})}
    before = evaluate_generated_timetable_candidate("before", sections, {}, {}, mapping, {})
    after = evaluate_generated_timetable_candidate("after", compact, {}, {}, mapping, {})
    assert before.lexicographic_score == after.lexicographic_score == (0,) * 9
    assert evaluation_is_better(after, before)
    ranked = rank_timetable_candidates(
        [{"id": "a-loose", "sections": sections}, {"id": "z-compact", "sections": compact}],
        {},
        {},
        mapping,
        {},
    )
    assert ranked[0].candidate_id == "z-compact"


def test_day_gate_records_mandatory_repair_exception_but_blocks_later_increase():
    before = {"working_days_by_instructor": {"1": 2}}
    repaired = {"working_days_by_instructor": {"1": 3}}
    result = {
        "instructor_schedule_before_passes": before,
        "instructor_schedule_after_hard_repairs": repaired,
    }
    gate = optimiser_instructor_day_regression(before, repaired, result)
    assert not gate["blocked"]
    assert gate["repair_exceptions"][0]["reason"] == "mandatory_instructor_constraint_repair"
    assert optimiser_instructor_day_regression(
        before, {"working_days_by_instructor": {"1": 4}}, result
    )["blocked"]


def test_mandatory_repair_does_not_excuse_preexisting_search_day_regression():
    before = {"working_days_by_instructor": {"1": 2}}
    after = {"working_days_by_instructor": {"1": 3}}
    result = {
        "instructor_schedule_before_passes": after,
        "instructor_schedule_after_hard_repairs": after,
    }
    assert optimiser_instructor_day_regression(before, after, result)["blocked"]


@pytest.mark.parametrize(
    ("pre_idle", "repair_idle", "final_idle", "blocked"),
    [(30, 60, 60, False), (30, 60, 75, True), (45, 60, 60, True), (30, None, 60, True)],
)
def test_idle_gate_allows_only_explicit_measured_mandatory_repair(
    pre_idle, repair_idle, final_idle, blocked
):
    before = evaluation(idle=30).quality_score["instructor_schedule"]
    after = evaluation(idle=final_idle).quality_score["instructor_schedule"]
    result = {
        "instructor_schedule_before_passes": evaluation(idle=pre_idle).quality_score[
            "instructor_schedule"
        ],
        "instructor_schedule_after_hard_repairs": (
            evaluation(idle=repair_idle).quality_score["instructor_schedule"]
            if repair_idle is not None
            else {}
        ),
    }
    gate = optimiser_instructor_idle_regression(before, after, result)
    assert gate["blocked"] is blocked
    if not blocked:
        assert len(gate["repair_exceptions"]) == 2  # Teacher and aggregate.
        assert all(
            r["reason"] == "mandatory_instructor_constraint_repair"
            for r in gate["repair_exceptions"]
        )


def test_missing_idle_measurement_cannot_be_excused_by_a_hard_repair():
    before = evaluation(idle=30).quality_score["instructor_schedule"]
    after = evaluation(idle=60).quality_score["instructor_schedule"]
    repaired = dict(after)
    after.pop("idle_minutes_by_instructor")
    gate = optimiser_instructor_idle_regression(
        before, after, {"instructor_schedule_after_hard_repairs": repaired}
    )
    assert gate["blocked"]
    assert any(r["metric"].endswith("_evidence") for r in gate["regressions"])


@pytest.mark.django_db
@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_runner_restores_actual_board_when_total_idle_gain_hides_teacher_regression(monkeypatch):
    from core.models import DeliveryBoard, SectionPlacement, TermSection, TimetableScenario
    from core.services import timetable_optimizer_v2 as optimizer
    from core.services import timetable_v2_runner as runner
    from core.services.timetable_board_persistence import snapshot_scenario

    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Idle ceiling")
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    for code, start, end in (
        ("A", "09:00", "10:00"),
        ("B", "12:00", "13:00"),
        ("C", "09:00", "10:00"),
        ("D", "10:00", "11:00"),
    ):
        ts = TermSection.objects.create(
            scenario=scenario,
            course_key=code,
            course_code=code,
            course_number=code,
            course_name=code,
            section="S1",
            available_capacity=30,
        )
        SectionPlacement.objects.create(
            board=board, term_section=ts, day="SUN", start_time=start, end_time=end
        )
    teacher_map = {f"{code}_S1": frozenset({1 if code in "AB" else 2}) for code in "ABCD"}
    monkeypatch.setattr(
        optimizer, "build_section_instructor_map_for_scenario", lambda _sid: teacher_map
    )
    monkeypatch.setattr(runner, "compute_scenario_safety_summary", lambda _sid: {})
    original = snapshot_scenario(scenario.id)

    def mutate(**kwargs):
        SectionPlacement.objects.filter(board=board, term_section__course_code="B").update(
            start_time="10:00", end_time="11:00"
        )
        SectionPlacement.objects.filter(board=board, term_section__course_code="D").update(
            start_time="10:30", end_time="11:30"
        )
        return {"baseline_score": [0] * 9, "final_score": [0] * 9}

    monkeypatch.setattr(optimizer, "optimise_current_timetable", mutate)
    result = runner.run_v2_optimisation_guarded(scenario.id)
    assert result["safety_blocked"]
    rows = result["safety_regression"]["regressions"]
    assert any(
        r.get("instructor_id") == "2" and r["metric"] == "instructor_idle_minutes" for r in rows
    )
    assert result["instructor_schedule_metric"]["before"]["idle_minutes"] == 120
    assert result["candidate_instructor_schedule"]["idle_minutes"] == 30
    assert result["instructor_schedule_metric"]["after"]["idle_minutes"] == 120
    assert snapshot_scenario(scenario.id) == original


@pytest.mark.django_db
@override_settings(
    TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True,
    TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=False,
    TIMETABLE_TIERED_OBJECTIVE_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=False,
    TIMETABLE_INSTRUCTOR_CLASH_ENABLED=False,
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=False,
)
def test_optimise_persists_instructor_gain_when_public_score_is_unchanged(monkeypatch):
    from core.models import (
        CourseInstructor,
        DeliveryBoard,
        Instructor,
        SectionPlacement,
        TermSection,
        TimetableScenario,
    )
    from core.services import timetable_optimizer_v2 as optimizer
    from core.services.timetable_assignment_models import RiskTier, StudentProfile

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="instructor priority", gender="M", programs=["CS"]
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    teacher = Instructor.objects.create(full_name="Priority Teacher", normalised_name="priority")
    for code, start, end in (("CS101", "09:00", "10:00"), ("CS102", "12:00", "13:00")):
        term_section = TermSection.objects.create(
            scenario=scenario,
            course_key=code,
            course_code=code,
            course_number=code,
            course_name=code,
            section="S1",
            available_capacity=30,
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=term_section,
            day="SUN",
            start_time=start,
            end_time=end,
        )
        CourseInstructor.objects.create(
            program="CS", course_code=code, section="M", instructor=teacher, role="primary"
        )
    monkeypatch.setattr(
        optimizer,
        "build_student_profiles_for_scenario",
        lambda _sid: {
            "s": StudentProfile("s", "CS", ["CS101"], RiskTier.C, 0),
        },
    )
    monkeypatch.setattr(optimizer, "build_course_rigidity_for_scenario", lambda _sid: {})
    monkeypatch.setattr(optimizer, "_build_pattern_catalog_for_scenario", lambda _sid: {})

    def improve(**kwargs):
        sections = kwargs["sections_by_id"]
        sections["CS102_S1"].meetings = [SectionMeeting(0, 600, 660)]
        return evaluate_generated_timetable_candidate(
            "compact",
            list(sections.values()),
            kwargs["student_profiles"],
            {},
            kwargs["section_instructor_ids"],
            kwargs["course_tiers"],
        )

    monkeypatch.setattr(
        "core.services.timetable_local_search_v2.diagnostic_driven_local_search", improve
    )
    result = optimizer.optimise_current_timetable(
        scenario.id, run_chain_search=False, run_cpsat_polish=False
    )
    assert result["final_score"] == result["baseline_score"]
    assert result["persist_result"]["action"] == "updated_in_place"
    assert result["quality_score"]["instructor_schedule"]["idle_minutes"] == 0
    assert (
        SectionPlacement.objects.get(board=board, term_section__course_code="CS102").start_time
        == "10:00"
    )
