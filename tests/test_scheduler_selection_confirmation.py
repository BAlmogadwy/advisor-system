"""Portfolio choice must protect instructors and confirm deliverable seats."""

from dataclasses import replace

import pytest

from scheduler.domain import (
    CapacityPolicy,
    DeliveryMode,
    Grid,
    MeetingKind,
    MeetingRequirement,
    Offering,
    Section,
    Snapshot,
    StudentDemand,
    TimeWindow,
)
from scheduler.domain.board import Board, Placement
from scheduler.domain.calendar import Day
from scheduler.seating import SeatingResult, seat_students
from scheduler.solve import (
    SolveResult,
    _days_by_instructor,
    choose_run,
    plan_portfolio,
    reason_to_discard_gap_pass,
)


def _run(name, *, idle=100, clashes=10, seating=None, **facts):
    return {
        "result": name,
        "days": [3, 3],
        "unroomed": 0,
        "astray": 0,
        "metrics": {"idle_minutes": idle},
        "clashes": clashes,
        "seating": seating,
        **facts,
    }


def _confirmation(*, missed=0, clashes=0, status="OPTIMAL"):
    return {
        "solution_found": status in {"OPTIMAL", "FEASIBLE"},
        "status": status,
        "unseated_demands": missed,
        "total_clashes": clashes,
        "students_with_a_clash": clashes,
    }


def test_default_portfolio_cannot_buy_five_extra_idle_minutes_with_student_clashes():
    chosen = choose_run(
        [_run("short gaps", idle=100, clashes=90), _run("less proxy harm", idle=105, clashes=0)]
    )
    assert chosen["result"] == "short gaps"


def test_actual_seating_breaks_only_an_instructor_priority_tie():
    good = _confirmation(clashes=0)
    bad = _confirmation(clashes=4)
    assert (
        choose_run(
            [_run("proxy", clashes=0, seating=bad), _run("actual", clashes=9, seating=good)]
        )["result"]
        == "actual"
    )
    assert (
        choose_run(
            [_run("short gaps", idle=100, seating=bad), _run("actual", idle=101, seating=good)]
        )["result"]
        == "short gaps"
    )


def test_feasible_confirmation_beats_unknown_zero_metrics():
    known = _confirmation(clashes=4, status="FEASIBLE")
    unknown = _confirmation(status="UNKNOWN")
    assert (
        choose_run(
            [
                _run("timeout", clashes=0, seating=unknown),
                _run("confirmed", clashes=9, seating=known),
            ]
        )["result"]
        == "confirmed"
    )


def test_unseated_demands_outrank_actual_clash_savings():
    assert (
        choose_run(
            [
                _run("unserved", seating=_confirmation(missed=1, clashes=0)),
                _run("all served", seating=_confirmation(missed=0, clashes=10)),
            ]
        )["result"]
        == "all served"
    )


@pytest.mark.parametrize("bad_fact", [{"violations": 1}, {"unplaced": 1}])
def test_missing_or_invalid_meetings_cannot_manufacture_the_best_instructor_week(bad_fact):
    bad = _run("broken", idle=0, days=[], **bad_fact)
    assert choose_run([bad, _run("complete", idle=900)])["result"] == "complete"


def _snapshot(*, weekly=1, sections=True):
    req = MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.ONLINE, 75, weekly)
    offerings = tuple(
        Offering(
            id=oid,
            course_code=oid,
            course_name=oid,
            credit_hours=3,
            programs=frozenset({"AI"}),
            terms=frozenset({1}),
            requirements=(req,),
            capacity=4,
            capacity_is_declared=True,
        )
        for oid in ("a", "b")
    )
    return Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=Grid.from_spec(
            lecture_starts={}, lab_starts={}, online_starts={"09:00": 75, "10:30": 75}
        ),
        offerings=offerings,
        sections=(Section("a1", "a", 1, 1), Section("a2", "a", 2, 3), Section("b1", "b", 1, 4))
        if sections
        else (),
        rooms=(),
        instructors=(),
        demand=tuple(StudentDemand(n, "AI", frozenset({"a", "b"})) for n in range(4)),
        policy=CapacityPolicy(4),
        source_fingerprint="fixture",
        created_at="2026-10-10",
    )


def _p(section, offering, day, *, index=1, start=540):
    return Placement(
        section,
        offering,
        index,
        MeetingKind.LECTURE,
        DeliveryMode.ONLINE,
        day,
        TimeWindow(start, start + 75),
    )


def _boards():
    small_collision = Board(
        (_p("a1", "a", Day.SUN), _p("a2", "a", Day.MON), _p("b1", "b", Day.SUN))
    )
    large_collision = Board(
        (_p("a1", "a", Day.MON), _p("a2", "a", Day.SUN), _p("b1", "b", Day.SUN))
    )
    return small_collision, large_collision


def test_capacity_aware_confirmation_distinguishes_the_actual_timetables():
    snap = _snapshot()
    first, second = (seat_students(snap, b, time_limit_seconds=3) for b in _boards())
    assert first.solution_found and second.solution_found
    assert first.unseated_demands == second.unseated_demands == 0
    assert first.total_clashes == 1
    assert second.total_clashes == 3


@pytest.mark.parametrize("mode", ["absent", "incomplete", "duplicate", "no sections"])
def test_only_complete_sections_offer_a_real_seat(mode):
    snap = _snapshot(weekly=2, sections=mode != "no sections")
    placements = ()
    if mode == "incomplete":
        placements = (_p("a1", "a", Day.SUN),)
    elif mode == "duplicate":
        placements = (_p("a1", "a", Day.SUN), _p("a1", "a", Day.MON))
    seating = seat_students(snap, Board(placements), time_limit_seconds=3)
    assert seating.solution_found
    assert seating.unseated_demands == 8
    assert seating.seated_demands == seating.students_fully_seated == 0
    assert seating.clash_free_percent is None


def test_timeout_summary_never_publishes_zero_actual_harm():
    seating = SeatingResult(4, 0, 8, 0, 0, 0, False)
    summary = seating.summary()
    assert summary["solution_found"] is False
    assert summary["total_clashes"] is None
    assert summary["unseated_demands"] is None
    assert summary["clash_free_percent"] is None
    assert summary["average_idle_minutes"] is None
    assert summary["regular_idle_minutes_avg"] is None


def test_partial_students_with_a_clash_do_not_reduce_the_fully_seated_percentage():
    seating = SeatingResult(
        2, 3, 1, 1, 1, 0, True, students_fully_seated=1, fully_seated_students_with_a_clash=0
    )
    assert seating.clash_free_percent == 100.0


def test_portfolio_confirms_only_finalists_and_keeps_seed_diversity(monkeypatch):
    import scheduler.seating as seating_module
    import scheduler.solve as solve_module

    snap = _snapshot()
    small, large = _boards()
    results = [
        SolveResult(small, "FEASIBLE", False, 90, 1),
        SolveResult(large, "FEASIBLE", False, 0, 1),
        SolveResult(
            replace(large, placements=large.placements[:2]),
            "FEASIBLE",
            False,
            0,
            1,
            unplaced=("b1#M1",),
        ),
    ]
    seeds_seen = []

    def fake_plan(snapshot, *, seed, **kwargs):
        seeds_seen.append(seed)
        return results[seed - 1]

    monkeypatch.setattr(solve_module, "plan", fake_plan)
    monkeypatch.setattr(solve_module, "expected_clashes", lambda s, b: 90 if b == small else 0)
    original = seating_module.seat_students
    calls = []

    def confirm(snapshot, board, *, time_limit_seconds):
        calls.append((board, time_limit_seconds))
        return original(snapshot, board, time_limit_seconds=time_limit_seconds)

    monkeypatch.setattr(seating_module, "seat_students", confirm)
    chosen = plan_portfolio(snap, seeds=(1, 2, 3), time_limit_seconds=2, confirmation_seconds=6)
    assert seeds_seen == [1, 2, 3]
    assert len(calls) == 2
    assert calls[0][1] <= 3
    assert chosen.board == small
    assert chosen.seating_confirmation["total_clashes"] == 1
    assert chosen.seating_confirmation["solution_found"] is True
    assert any("actual clashes" in note for note in chosen.notes)


def test_a_failed_portfolio_does_not_spend_an_unrequested_fourth_attempt(monkeypatch):
    import scheduler.solve as solve_module

    seeds_seen = []

    def empty_plan(snapshot, *, seed, **kwargs):
        seeds_seen.append(seed)
        return SolveResult(Board(()), "UNKNOWN", False, 0, 1)

    monkeypatch.setattr(solve_module, "plan", empty_plan)
    result = plan_portfolio(_snapshot(), seeds=(1, 2, 3), time_limit_seconds=1)
    assert seeds_seen == [1, 2, 3]
    assert result.status == "UNKNOWN"
    assert not result.board.placements


def test_online_weekdays_cannot_make_one_campus_day_look_worse_than_two():
    snap = _snapshot()
    one_campus = Board(
        (
            replace(_p("a1", "a", Day.SUN), delivery=DeliveryMode.IN_PERSON, instructor_id=1),
            replace(_p("a2", "a", Day.MON), instructor_id=1),
            replace(_p("b1", "b", Day.TUE), instructor_id=1),
        )
    )
    two_campus = Board(
        (
            replace(_p("a1", "a", Day.SUN), delivery=DeliveryMode.IN_PERSON, instructor_id=1),
            replace(_p("a2", "a", Day.MON), delivery=DeliveryMode.IN_PERSON, instructor_id=1),
            replace(_p("b1", "b", Day.MON, start=630), instructor_id=1),
        )
    )
    before = _days_by_instructor(snap, one_campus)
    after = _days_by_instructor(snap, two_campus)
    assert before == {1: 1}
    assert after == {1: 2}
    chosen = choose_run(
        [
            _run("one commute", days=list(before.values()), idle=100),
            _run("two commutes", days=list(after.values()), idle=0),
        ]
    )
    assert chosen["result"] == "one commute"
    assert "lengthened the week" in reason_to_discard_gap_pass(
        days_before=before,
        days_after=after,
        rooms_before=0,
        rooms_after=0,
        idle_before=100,
        idle_after=0,
    )


def test_online_only_instructors_have_zero_campus_days():
    board = Board(
        (
            replace(_p("a1", "a", Day.SUN), instructor_id=1),
            replace(_p("b1", "b", Day.MON), instructor_id=1),
        )
    )
    assert _days_by_instructor(_snapshot(), board) == {1: 0}


def test_solve_summary_exposes_stored_confirmation_without_another_solve():
    confirmation = _confirmation(clashes=1, status="FEASIBLE")
    result = SolveResult(Board(()), "FEASIBLE", False, 0, 1, seating_confirmation=confirmation)
    assert result.summary()["student_seating"] is confirmation
    assert SolveResult(Board(()), "UNKNOWN", False, 0, 1).summary()["student_seating"] is None
