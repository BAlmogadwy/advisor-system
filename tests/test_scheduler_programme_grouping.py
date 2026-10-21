"""The scheduler's exact seating honours "mainly for programme" marks (R5).

Generate confirms its board with ``scheduler.seating.seat_students``. With no
mark that is exactly today's seating. With marks a second solve groups the
students, and it can never cost a seat: every phase-1 unseated flag is pinned
and no student's clashes may rise. The bridge passes the marks only under the
flag, re-confirms a portfolio's chosen board without letting it get worse, and
carries every mark across the rebuild of its own sections by (course, label),
reporting any it had to drop.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import override_settings

from core.services import timetable_student_assignment as ssa
from scheduler import bridge
from scheduler import seating as seating_module
from scheduler.domain import Section, StudentDemand, TimeWindow
from scheduler.domain.board import Board
from scheduler.domain.calendar import Day
from scheduler.seating import grouping_cost, seat_students
from scheduler.solve import SolveResult
from tests.test_scheduler_rules import ROOMY, _grid_snapshot, _lecture, _offering, _p

W9, W1030 = TimeWindow(540, 615), TimeWindow(630, 705)


def _one_course(capacities, students, *, days=(Day.SUN, Day.MON)):
    """One single-meeting course, one section per capacity, students by programme."""
    sections = [Section(f"ai236#S{i}", "ai236", i, cap) for i, cap in enumerate(capacities, 1)]
    demand = [
        StudentDemand(student_id=n, program=programme, offering_ids=frozenset({"ai236"}))
        for n, programme in enumerate(students, 1)
    ]
    snap = _grid_snapshot(ROOMY, [_offering("ai236", reqs=_lecture(1))], sections, demand=demand)
    board = Board(
        tuple(
            _p(section=s.id, offering="ai236", day=days[i % len(days)], window=W9)
            for i, s in enumerate(sections)
        )
    )
    return snap, board


def _clash_toy():
    """offa S2 (marked AI2) meets with offb; offa S1 does not."""
    offerings = [_offering("offa", reqs=_lecture(1)), _offering("offb", reqs=_lecture(1))]
    sections = [
        Section("offa#S1", "offa", 1, 5),
        Section("offa#S2", "offa", 2, 5),
        Section("offb#S1", "offb", 1, 5),
    ]
    demand = [StudentDemand(student_id=1, program="AI2", offering_ids=frozenset({"offa", "offb"}))]
    snap = _grid_snapshot(ROOMY, offerings, sections, demand=demand)
    board = Board(
        (
            _p(section="offa#S1", offering="offa", day=Day.SUN, window=W9),
            _p(section="offa#S2", offering="offa", day=Day.SUN, window=W1030),
            _p(section="offb#S1", offering="offb", day=Day.SUN, window=W1030),
        )
    )
    return snap, board


HARD = ("unseated_demands", "total_clashes", "students_with_a_clash", "seated_demands")


# ── no marks: today's seating, today's summary ──────────────────────────


@pytest.mark.parametrize("marks", [None, {}, {"ai236#S1": "  "}])
def test_without_marks_the_seating_and_summary_are_todays(marks, monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("phase 2 ran without a mark")

    monkeypatch.setattr(seating_module, "_group_by_programme", refuse)
    snap, board = _one_course([5, 5], ["AI2"] * 5 + ["DS"] * 5)
    plain = seat_students(snap, board, time_limit_seconds=10).summary()
    summary = seat_students(snap, board, time_limit_seconds=10, preferred_programme=marks).summary()
    assert set(summary) == set(plain)
    assert "programme_grouping" not in summary
    assert {k: summary[k] for k in HARD} == {k: plain[k] for k in HARD}


# ── marks: grouped, and never a seat ─────────────────────────────────────


def test_marks_group_the_students_and_cost_nothing() -> None:
    snap, board = _one_course([5, 5], ["AI2"] * 5 + ["DS"] * 5)
    plain = seat_students(snap, board, time_limit_seconds=10).summary()
    summary = seat_students(
        snap, board, time_limit_seconds=10, preferred_programme={"ai236#S2": "ai2"}
    ).summary()
    assert {k: summary[k] for k in HARD} == {k: plain[k] for k in HARD}
    grouping = summary["programme_grouping"]
    assert grouping["marked_sections"] == ["ai236#S2"]
    assert grouping["grouping_cost_after"] == 0
    assert grouping["status"] in {"OPTIMAL", "FEASIBLE", "NOT_NEEDED"}


def test_a_missed_seat_can_never_pay_for_a_better_grouping() -> None:
    """Both AI2 students fit only if one sits in DS's section. Unseating that
    student would make the grouping perfect; phase 1's seat is pinned."""
    snap, board = _one_course([1, 1], ["AI2", "AI2"])
    summary = seat_students(
        snap, board, time_limit_seconds=10, preferred_programme={"ai236#S1": "DS"}
    ).summary()
    assert summary["unseated_demands"] == 0
    assert summary["programme_grouping"]["grouping_cost_after"] == 1
    assert summary["programme_grouping"]["status"] in {"OPTIMAL", "FEASIBLE"}


def test_a_clash_can_never_pay_for_a_better_grouping() -> None:
    snap, board = _clash_toy()
    summary = seat_students(
        snap, board, time_limit_seconds=10, preferred_programme={"offa#S2": "AI2"}
    ).summary()
    assert summary["total_clashes"] == 0
    assert summary["students_with_a_clash"] == 0
    assert summary["programme_grouping"]["grouping_cost_after"] == 1


def test_zero_grouping_seconds_keeps_phase_one() -> None:
    snap, board = _one_course([1, 1], ["AI2", "AI2"])
    summary = seat_students(
        snap,
        board,
        time_limit_seconds=10,
        preferred_programme={"ai236#S1": "DS"},
        grouping_seconds=0,
    ).summary()
    grouping = summary["programme_grouping"]
    assert grouping["status"] == "NOT_RUN"
    assert grouping["grouping_cost_after"] == grouping["grouping_cost_before"] == 1
    with pytest.raises(ValueError, match="grouping_seconds"):
        seat_students(snap, board, preferred_programme={"ai236#S1": "DS"}, grouping_seconds=-1)


def test_the_cost_table_is_the_classic_seaters() -> None:
    table = {
        # (student's programme, section mark, own section exists) -> cost
        ("AI2", "AI2", True): 0,
        ("AI2", "", True): 1,
        ("AI2", "DS", True): 2,
        ("AI2", "", False): 0,
        ("AI2", "DS", False): 1,
        ("", "", False): 0,
        ("", "AI2", False): 1,
    }
    for (programme, mark, own), cost in table.items():
        assert grouping_cost(programme, mark, own) == cost, (programme, mark, own)
        assert ssa.programme_grouping_cost(programme, mark, own) == cost, (programme, mark, own)


# ── the bridge ───────────────────────────────────────────────────────────


@pytest.fixture
def toy_pipeline(monkeypatch):
    """The real persist step around a stubbed snapshot, solve and measures."""
    from core.models import DeliveryBoard, TimetableScenario

    snap, board = _one_course([5, 5], ["AI2"] * 5 + ["DS"] * 5)
    calls: list[dict] = []
    real_seat = bridge.seat_students

    def seat(snapshot, board, **kwargs):
        calls.append(kwargs)
        return real_seat(snapshot, board, **kwargs)

    def solved(*_args, **_kwargs):
        return SolveResult(
            board=board,
            status="FEASIBLE",
            proven_optimal=False,
            expected_clashes=0.0,
            wall_time_seconds=0.0,
        )

    monkeypatch.setattr(bridge, "build_snapshot", lambda **_kwargs: snap)
    monkeypatch.setattr(bridge, "assign_instructors", lambda snapshot: snapshot)
    monkeypatch.setattr(bridge, "plan", solved)
    monkeypatch.setattr(bridge, "assign_rooms_exact", lambda _s, b, **_k: b)
    monkeypatch.setattr(bridge, "seat_students", seat)
    monkeypatch.setattr(
        bridge,
        "validate",
        lambda *_a: SimpleNamespace(violation_count=0, certification=SimpleNamespace(value="ok")),
    )
    monkeypatch.setattr(
        bridge, "room_shortfall", lambda *_a: {"unroomed": 0, "impossible": 0, "saturated": 0}
    )
    monkeypatch.setattr(
        bridge,
        "instructor_metrics",
        lambda *_a: {"working_days": 0, "floor_days": 0, "idle_minutes": 0},
    )
    monkeypatch.setattr(
        bridge, "sibling_adjacency", lambda *_a: {"pairs_back_to_back": 0, "pairs_achievable": 0}
    )
    monkeypatch.setattr(
        bridge,
        "time_of_day_drift",
        lambda *_a: {"percent_same_slot": 0, "percent_within_one_slot": 0, "mean_drift_minutes": 0},
    )
    monkeypatch.setattr(bridge, "expected_clashes", lambda *_a: 0.0)
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="bridge-marks", gender="M"
    )
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    return SimpleNamespace(scenario=scenario, calls=calls, solved=solved)


def _build(scenario, **kwargs):
    return bridge.build_into_scenario(
        scenario.id,
        academic_year="1448",
        term=2,
        programs=["AI"],
        gender="M",
        confirmation_seconds=10,
        **kwargs,
    )


def _marked(scenario, label, mark):
    from core.models import TermSection
    from core.services.course_identity import planner_course_key

    return TermSection.objects.create(
        scenario=scenario,
        source_tag=bridge.SOURCE_TAG,
        course_key=planner_course_key("AI236", "ai236"),
        course_code="AI236",
        course_number="AI236",
        section=label,
        preferred_programme=mark,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("enabled", [False, True])
def test_generate_carries_every_mark_across_the_rebuild_and_reports_a_dropped_one(
    toy_pipeline, enabled
) -> None:
    from core.models import TermSection

    scenario = toy_pipeline.scenario
    _marked(scenario, "S2", "AI2")
    _marked(scenario, "S3", "DS")  # this run plans two sections: S3 is gone
    with override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=enabled):
        summary = _build(scenario, runs=1)

    marks = dict(
        TermSection.objects.filter(scenario=scenario).values_list("section", "preferred_programme")
    )
    assert marks == {"S1": "", "S2": "AI2"}
    assert summary["programme_preferences_dropped"] == [
        {"course_key": "AI236", "section": "S3", "programme": "DS"}
    ]
    assert any("dropped: AI236 S3 (DS)" in warning for warning in summary["warnings"])
    # The single run's confirmation is the grouped one, only under the flag.
    assert [call.get("preferred_programme") for call in toy_pipeline.calls] == (
        [{"ai236#S2": "AI2"}] if enabled else [None]
    )
    grouping = summary["student_seating"].get("programme_grouping")
    assert (grouping is not None) == enabled
    if enabled:
        assert grouping["grouping_cost_after"] == 0


@pytest.mark.django_db
def test_a_generate_with_no_mark_keeps_its_summary(toy_pipeline) -> None:
    with override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=True):
        summary = _build(toy_pipeline.scenario, runs=1)
    assert "programme_preferences_dropped" not in summary
    assert "programme_grouping" not in summary["student_seating"]
    assert [call.get("preferred_programme") for call in toy_pipeline.calls] == [None]


@pytest.mark.django_db
@pytest.mark.parametrize("worse", [False, True])
def test_a_portfolio_board_is_regrouped_only_when_no_worse_for_students(
    toy_pipeline, monkeypatch, worse
) -> None:
    selected = {
        "solution_found": True,
        "unseated_demands": 0,
        "total_clashes": 0 if worse else 5,
        "students_with_a_clash": 0 if worse else 5,
        "status": "FEASIBLE",
    }

    def portfolio(*args, **kwargs):
        result = toy_pipeline.solved()
        result.seating_confirmation = dict(selected)
        return result

    monkeypatch.setattr(bridge, "plan_portfolio", portfolio)
    recorded_seat = bridge.seat_students  # the fixture's: records each call

    def seat(snapshot, board, **kwargs):
        result = recorded_seat(snapshot, board, **kwargs)
        if worse:
            result.total_clashes = 1  # as if this CP-SAT run were unluckier
        return result

    monkeypatch.setattr(bridge, "seat_students", seat)
    _marked(toy_pipeline.scenario, "S2", "AI2")
    with override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=True):
        summary = _build(toy_pipeline.scenario, runs=3)
    seating = summary["student_seating"]
    assert [call.get("preferred_programme") for call in toy_pipeline.calls] == [{"ai236#S2": "AI2"}]
    if worse:
        assert seating["total_clashes"] == 0  # the selection's figures stand
        assert seating["programme_grouping"]["status"] == "NOT_CONFIRMED"
    else:
        assert seating["total_clashes"] == 0 < selected["total_clashes"]
        assert seating["programme_grouping"]["grouping_cost_after"] == 0


def test_a_failed_regrouping_never_replaces_a_found_selection() -> None:
    found = {
        "solution_found": True,
        "unseated_demands": 1,
        "total_clashes": 2,
        "students_with_a_clash": 1,
    }
    assert bridge._no_worse(dict(found), found)
    assert not bridge._no_worse({"solution_found": False}, found)
    assert not bridge._no_worse({**found, "unseated_demands": 2}, found)
    assert not bridge._no_worse({**found, "students_with_a_clash": 2}, found)
    assert bridge._no_worse(found, {"solution_found": False})
