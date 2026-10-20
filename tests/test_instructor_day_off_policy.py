"""The day-off policy's acceptance relation (owner rulings R1-R5, 2026-10-19).

Pure: evaluations are built by hand. Each test is an accept/reject pair at the
boundary of one ruling; the property tests use a seeded ``random.Random``.
"""

from __future__ import annotations

import json
import random
from itertools import permutations
from pathlib import Path

import pytest
from django.test import override_settings

from core.services import timetable_optimisation_policy as policy_module
from core.services.timetable_assignment_models import TimetableEvaluationResult
from core.services.timetable_optimisation_policy import (
    MAX_WORKING_DAYS,
    SPARE_DAY_IDLE,
    TEACHING_WEEK,
    DayOffAnchor,
    DayOffPolicy,
    current_day_off_policy,
    day_off_scope,
    evaluation_is_better,
    evaluation_order_key,
    is_day_off_policy_active,
    stage_is_better,
)
from tests.day_off_corpus import legacy_corpus, legacy_outputs

GOLDEN = Path(__file__).parent / "fixtures" / "day_off_legacy_policy_golden.json"

# (h0, h1, h2, T2-over, gap, soft, reserve, spread, instructor idle penalty)
START = (0, 0, 0, 10, 1000, 5, 0, 0, 0)


def student(**changes: int) -> tuple[int, ...]:
    """``START`` with positions changed by name: hard0..hard3, gap, soft."""
    names = ("hard0", "hard1", "hard2", "hard3", "gap", "soft", "reserve", "spread", "instr")
    score = list(START)
    for name, delta in changes.items():
        score[names.index(name)] += delta
    return tuple(score)


def schedule(days: dict, idle: dict) -> dict:
    return {
        "working_days": sum(days.values()),
        "idle_minutes": sum(idle.values()),
        "working_days_by_instructor": dict(days),
        "idle_minutes_by_instructor": dict(idle),
        "affected_instructors": len(days),
    }


def board(days: dict, idle: dict | None = None, score=START, overload: int = 0):
    idle = dict.fromkeys(days, 0) if idle is None else idle
    return TimetableEvaluationResult(
        "test",
        tuple(score),
        {},
        [],
        [],
        [],
        [],
        quality_score={"instructor_schedule": schedule(days, idle)},
        instructor_overload_count=overload,
    )


def policy_at(days: dict, idle: dict | None = None, score=START, *, saved_idle=0, padded=True):
    idle = dict.fromkeys(days, 0) if idle is None else idle
    anchor = DayOffAnchor(
        days=dict(days),
        idle=dict(idle),
        total_idle=sum(idle.values()),
        padded=padded,
        saved_idle=saved_idle,
    )
    return DayOffPolicy(anchor, tuple(score), "test")


def start_board(policy: DayOffPolicy) -> TimetableEvaluationResult:
    return board(policy.anchor.days, policy.anchor.idle, policy.score)


def accepts(policy: DayOffPolicy, candidate, base=None, incumbent=None) -> bool:
    """One move from ``base`` (default: the run start), checked both ways in."""
    base = start_board(policy) if base is None else base
    incumbent = base if incumbent is None else incumbent
    direct = policy.step(candidate, incumbent, base=base)
    with day_off_scope(policy):  # the same relation through the shared entry point
        assert evaluation_is_better(candidate, incumbent, feasibility_ceilings=base) is direct
    return direct


# ── R1: never ADD a 5th working day ───────────────────────────────────────


def test_constants_are_the_owner_rulings():
    assert (TEACHING_WEEK, MAX_WORKING_DAYS, SPARE_DAY_IDLE) == (5, 4, 60)


def test_t1_a_fifth_day_is_never_added_even_for_a_hard_gain():
    policy = policy_at({"X": 4})
    fifth = board({"X": 5}, score=student(hard3=-1))
    assert not accepts(policy, fifth)
    assert not policy.core(fifth, start_board(policy))

    policy = policy_at({"X": 3})
    fourth = board({"X": 4}, score=student(hard3=-1))
    assert accepts(policy, fourth)
    assert policy.core(fourth, start_board(policy))


def test_t2_a_fifth_day_given_back_never_returns():
    """A teacher on 5 days at the start, on 4 in the board in hand: the 5th day
    may not come back, even for a hard student gain."""
    policy = policy_at({"X": 5, "Y": 2}, {"X": 0, "Y": 100})
    base = board({"X": 4, "Y": 2}, {"X": 0, "Y": 100}, student(gap=-10))
    back = board({"X": 5, "Y": 2}, {"X": 0, "Y": 0}, student(hard3=-1))
    assert not accepts(policy, back, base=base)
    # Kept at 5 all along, the teacher's week is ranked normally.
    still = board({"X": 5, "Y": 2}, {"X": 0, "Y": 40})
    assert accepts(policy, still)


def test_t3_a_first_day_off_ranks_above_idle_and_above_students():
    """X gets a first day off while Y's idle returns to its start and students
    lose 500 gap minutes: no breach, so fives decide before idle and gap."""
    policy = policy_at({"X": 5, "Y": 3}, {"X": 100, "Y": 300})
    base = board({"X": 5, "Y": 3}, {"X": 100, "Y": 0})
    first_day_off = board({"X": 4, "Y": 3}, {"X": 100, "Y": 200}, student(gap=500))
    assert accepts(policy, first_day_off, base=base)


# ── R2: a spare day off only for a student gain ───────────────────────────


def test_t4_a_spare_day_needs_a_strict_gain_judged_position_by_position():
    policy = policy_at({"X": 3})
    assert not accepts(policy, board({"X": 4}))  # students unchanged
    assert not accepts(policy, board({"X": 4}, score=student(hard0=-1, hard1=1)))
    # A gap-only gain buys it, with R3's minutes netted out of the idle rank.
    assert accepts(policy, board({"X": 4}, {"X": 40}, student(gap=-1)))


def test_t4_the_gain_is_strict_against_the_run_start_too():
    """The board in hand traded 20 gap minutes for Y's idle. Giving X a spare
    day to win them back only returns students to the start: no gain."""
    policy = policy_at({"X": 3, "Y": 2}, {"X": 0, "Y": 100})
    base = board({"X": 3, "Y": 2}, {"X": 0, "Y": 50}, student(gap=20))
    back_to_start = board({"X": 4, "Y": 2}, {"X": 0, "Y": 50}, START)
    assert not accepts(policy, back_to_start, base=base)
    assert accepts(policy, board({"X": 4, "Y": 2}, {"X": 0, "Y": 50}, student(gap=-1)), base=base)


def test_t5_r2_holds_per_move_and_against_the_run_start():
    policy = policy_at({"X": 3, "Y": 2}, {"X": 0, "Y": 100})
    base = board({"X": 3, "Y": 2}, {"X": 0, "Y": 100}, student(gap=-100))
    # Still better than the start, worse than (or level with) the board in
    # hand: the move itself bought nothing for its spare day.
    worse_here = board({"X": 4, "Y": 2}, {"X": 0, "Y": 50}, student(gap=-50))
    assert not accepts(policy, worse_here, base=base)
    level_here = board({"X": 4, "Y": 2}, {"X": 0, "Y": 50}, student(gap=-100))
    assert not accepts(policy, level_here, base=base)
    # The day was bought (gap -10); a later move gives that gain back.
    given = board({"X": 4, "Y": 2}, {"X": 0, "Y": 100}, student(gap=-10))
    assert accepts(policy, given)
    gain_spent = board({"X": 4, "Y": 2}, {"X": 0, "Y": 70}, student(gap=10))
    assert not accepts(policy, gain_spent, base=given)
    # The same move without the spare day is an ordinary idle gain.
    assert accepts(policy, board({"X": 3, "Y": 2}, {"X": 0, "Y": 70}, student(gap=10)))


# ── R3: at most 60 idle minutes per spare day given up ────────────────────


@pytest.mark.parametrize(
    ("start_days", "minutes", "accepted"),
    [(3, 60, True), (3, 61, False), (2, 120, True), (2, 121, False)],
)
def test_t6_r3_is_exactly_sixty_minutes_per_day(start_days, minutes, accepted):
    policy = policy_at({"X": start_days})
    assert accepts(policy, board({"X": 4}, {"X": minutes}, student(gap=-1))) is accepted


@pytest.mark.parametrize(("second", "accepted"), [(20, True), (21, False)])
def test_t7_r3_is_cumulative_against_the_run_start(second, accepted):
    policy = policy_at({"X": 3, "Y": 3}, {"X": 0, "Y": 200})
    first = board({"X": 4, "Y": 3}, {"X": 40, "Y": 200}, student(gap=-5))
    assert accepts(policy, first)
    more = board({"X": 4, "Y": 3}, {"X": 40 + second, "Y": 100}, student(gap=-5))
    assert accepts(policy, more, base=first) is accepted


def test_t8_r3_is_absolute_a_large_total_fall_does_not_excuse_it():
    policy = policy_at({"X": 3, "Z": 3}, {"X": 0, "Z": 500})
    over = board({"X": 4, "Z": 3}, {"X": 61, "Z": 0}, student(gap=-1))
    assert not accepts(policy, over)
    assert accepts(policy, board({"X": 4, "Z": 3}, {"X": 60, "Z": 0}, student(gap=-1)))


# ── R4: one instructor's idle may rise only if the total strictly falls ───


@pytest.mark.parametrize(("first", "accepted"), [(69, True), (70, False)])
def test_t9_r4_needs_a_strictly_lower_total(first, accepted):
    """Teacher 2 gains 30: teacher 1 must lose MORE than 30. A student gain
    rides along, so a level total is refused by R4 itself, not by the rank."""
    policy = policy_at({"1": 2, "2": 2}, {"1": 100, "2": 0})
    after = board({"1": 2, "2": 2}, {"1": first, "2": 30}, student(gap=-1))
    assert accepts(policy, after) is accepted


@pytest.mark.parametrize(("fall", "accepted"), [(50, False), (71, True)])
def test_t10_r4_reads_the_raw_total_and_allowances_are_not_lent(fall, accepted):
    """The giver's +60 is sanctioned for the giver only: Y's +10 still needs the
    RAW total to fall, so Z must fall by more than both rises together."""
    policy = policy_at({"X": 3, "Y": 2, "Z": 2}, {"X": 0, "Y": 0, "Z": 200})
    after = board({"X": 4, "Y": 2, "Z": 2}, {"X": 60, "Y": 10, "Z": 200 - fall}, student(gap=-1))
    assert accepts(policy, after) is accepted


# ── The day trade's idle allowance: a FIRST day off only (owner, 2026-10-20) ─


@pytest.mark.parametrize(("minutes", "accepted"), [(255, True), (256, False)])
def test_t11_the_saved_day_allowance_covers_a_first_day_off(minutes, accepted):
    policy = policy_at({"Y": 5}, {"Y": 0}, saved_idle=255)
    assert accepts(policy, board({"Y": 4}, {"Y": minutes})) is accepted


@pytest.mark.parametrize(("other_fall", "accepted"), [(0, False), (1, False), (2, True)])
def test_t11_a_second_day_off_raises_idle_only_when_the_total_falls(other_fall, accepted):
    policy = policy_at({"X": 3, "W": 2}, {"X": 0, "W": 100}, saved_idle=255)
    after = board({"X": 2, "W": 2}, {"X": 1, "W": 100 - other_fall})
    assert accepts(policy, after) is accepted


def test_t11_without_the_day_trade_a_first_day_off_earns_no_idle():
    policy = policy_at({"Y": 5}, {"Y": 0}, saved_idle=0)
    assert not accepts(policy, board({"Y": 4}, {"Y": 1}))
    assert accepts(policy, board({"Y": 4}, {"Y": 0}))


@pytest.mark.parametrize(("w_fall", "accepted"), [(0, False), (15, False), (16, True)])
def test_t11_a_second_day_off_cannot_spend_what_the_first_left(w_fall, accepted):
    """Y started on 5 days; a first day off cost 15 of the 255 minutes. Freeing
    a second day for 15 more would still fit the 255, but that allowance is
    for the first day off only: the move needs the total to fall."""
    policy = policy_at({"Y": 5, "W": 2}, {"Y": 0, "W": 100}, saved_idle=255)
    first = board({"Y": 4, "W": 2}, {"Y": 15, "W": 100})
    assert accepts(policy, first)
    second = board({"Y": 3, "W": 2}, {"Y": 30, "W": 100 - w_fall}, student(gap=-1))
    assert accepts(policy, second, base=first) is accepted
    with day_off_scope(policy):
        # A stage boundary judges the stage's output, not each move.
        assert stage_is_better(second, first) is True


@pytest.mark.parametrize(("w_fall", "admitted"), [(0, False), (16, True)])
def test_t11_a_swap_cannot_spend_the_first_days_allowance_on_a_second(w_fall, admitted):
    policy = policy_at({"Y": 5, "W": 2}, {"Y": 0, "W": 100}, saved_idle=255)
    kept = schedule({"Y": 4, "W": 2}, {"Y": 15, "W": 100})
    swapped = schedule({"Y": 3, "W": 2}, {"Y": 30, "W": 100 - w_fall})
    assert policy.swap_ok(swapped, kept, score=START) is admitted


def test_t11_the_allowance_is_frozen_with_the_run_start(settings):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 255
    anchor = DayOffAnchor.from_metrics(schedule({"Y": 5}, {"Y": 0}), padded=True)
    policy = DayOffPolicy(anchor, START, "test")
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = False  # mid-run: no effect
    assert anchor.saved_idle == 255
    assert accepts(policy, board({"Y": 4}, {"Y": 255}))


# ── Evidence fails closed ─────────────────────────────────────────────────


def _broken(defect: str) -> TimetableEvaluationResult:
    after = board({"X": 3, "Y": 2}, {"X": 0, "Y": 0})
    metrics = after.quality_score["instructor_schedule"]
    if defect == "map":
        del metrics["idle_minutes_by_instructor"]
    elif defect == "teacher":
        del metrics["idle_minutes_by_instructor"]["Y"]
    elif defect == "null":
        metrics["idle_minutes_by_instructor"]["Y"] = None
    elif defect == "negative":
        metrics["idle_minutes_by_instructor"]["Y"] = -1
    elif defect == "bool":  # True would add up as one day
        metrics["working_days_by_instructor"]["Y"] = True
        metrics["working_days"] = 4
    elif defect == "total":
        metrics["idle_minutes"] = 5
    elif defect == "count":
        metrics["affected_instructors"] = 1
    elif defect == "outside":
        metrics["working_days_by_instructor"]["Q"] = 1
        metrics["idle_minutes_by_instructor"]["Q"] = 0
        metrics["working_days"] += 1
        metrics["affected_instructors"] = 3
    else:
        after.quality_score.clear()
    return after


@pytest.mark.parametrize(
    "defect", ["map", "teacher", "null", "negative", "bool", "total", "count", "outside", "all"]
)
@pytest.mark.parametrize("side", ["candidate", "base"])
def test_t12_incomplete_evidence_fails_closed(defect, side):
    policy = policy_at({"X": 3, "Y": 2}, {"X": 90, "Y": 30})
    good_base = start_board(policy)
    good_candidate = board({"X": 3, "Y": 2}, {"X": 0, "Y": 0})
    assert accepts(policy, good_candidate)
    candidate = _broken(defect) if side == "candidate" else good_candidate
    base = _broken(defect) if side == "base" else good_base
    assert not policy.step(candidate, base, base=base)
    assert not policy.core(candidate, base)
    if side == "candidate":
        assert policy.key(candidate)[4:6] == (float("inf"), float("inf"))
        assert not policy.swap_ok(
            candidate.quality_score.get("instructor_schedule", {}),
            schedule(policy.anchor.days, policy.anchor.idle),
            score=START,
        )


def test_t12_a_teacher_with_nothing_reads_zero_only_with_one_teacher_per_section():
    absent = board({"X": 3}, {"X": 0})  # Y teaches nothing on this board
    padded = policy_at({"X": 3, "Y": 2}, {"X": 0, "Y": 30})
    assert padded.measure(schedule({"X": 3}, {"X": 0}), START).days == {"X": 3, "Y": 0}
    assert accepts(padded, absent)
    union = policy_at({"X": 3, "Y": 2}, {"X": 0, "Y": 30}, padded=False)
    assert union.measure(schedule({"X": 3}, {"X": 0}), START) is None
    assert not accepts(union, absent)


# ── Order independence, composition, and the strict order ────────────────


def test_t13_alternatives_are_independent_of_enumeration_order():
    """The board in hand carries an R4 breach (Y's idle rose, the total did not
    fall). ``heals`` removes it; ``hard`` only shrinks it but gains a hard
    student position, so it ranks first. Judged against the board in hand both
    are admissible whatever order the scan meets them in."""
    policy = policy_at({"X": 2, "Y": 2}, {"X": 100, "Y": 0})
    base = board({"X": 2, "Y": 2}, {"X": 100, "Y": 30})
    heals = board({"X": 2, "Y": 2}, {"X": 100, "Y": 0})
    hard = board({"X": 2, "Y": 2}, {"X": 100, "Y": 20}, student(hard3=-1))
    worse = board({"X": 2, "Y": 2}, {"X": 100, "Y": 0}, student(hard0=1))
    louder = board({"X": 2, "Y": 2}, {"X": 100, "Y": 25})
    for alternatives in permutations((heals, hard, worse, louder)):
        winner = base
        for candidate in alternatives:
            if policy.step(candidate, winner, base=base):
                winner = candidate
        assert winner is hard


def test_t15_stage_boundaries_compose_where_moves_do_not():
    """Move 1 gives X a spare day for a student gain; move 2 trades some of the
    gain for Y's idle. Each is a legal step, the stage start and end differ
    only by X's day and a still-better score than the run start — the
    boundary admits what the stage legally reached."""
    policy = policy_at({"X": 3, "Y": 2}, {"X": 0, "Y": 100})
    stage_start = board({"X": 3, "Y": 2}, {"X": 0, "Y": 100}, student(gap=-100))
    move_1 = board({"X": 4, "Y": 2}, {"X": 0, "Y": 100}, student(gap=-110))
    move_2 = board({"X": 4, "Y": 2}, {"X": 0, "Y": 50}, student(gap=-95))
    assert accepts(policy, move_1, base=stage_start)
    assert accepts(policy, move_2, base=move_1)
    with day_off_scope(policy):
        assert stage_is_better(move_2, stage_start)
    assert not policy.step(move_2, stage_start, base=stage_start)


def _random_board(rng: random.Random, teachers: list[str]) -> TimetableEvaluationResult:
    days = {t: rng.randint(1, 5) for t in teachers}
    idle = {t: rng.choice((0, 30, 60, 90, 150, 300)) for t in teachers}
    score = (rng.randint(0, 1), 0, 0, rng.randint(0, 1), rng.randint(0, 3), 0, 0, 0, 0)
    return board(days, idle, score, overload=rng.randint(0, 1))


def test_t14_core_is_a_strict_partial_order_and_committed_chains_compose():
    teachers = ["1", "2", "3"]
    for seed in range(500):
        rng = random.Random(seed)
        start = _random_board(rng, teachers)
        start.instructor_overload_count = 1
        policy = DayOffPolicy(
            DayOffAnchor(
                days=dict(start.quality_score["instructor_schedule"]["working_days_by_instructor"]),
                idle=dict(start.quality_score["instructor_schedule"]["idle_minutes_by_instructor"]),
                total_idle=start.quality_score["instructor_schedule"]["idle_minutes"],
                padded=True,
                saved_idle=rng.choice((0, 255)),
            ),
            tuple(start.lexicographic_score),
            "test",
        )
        boards = [start] + [_random_board(rng, teachers) for _ in range(5)]
        for a in boards:
            assert not policy.core(a, a)
            for b in boards:
                for c in boards:
                    if policy.core(a, b) and policy.core(b, c):
                        assert policy.core(a, c), seed
        # A greedy scan, committing the best admissible move each iteration.
        current, commits = start, 0
        for _iteration in range(6):
            winner = current
            for candidate in (_random_board(rng, teachers) for _ in range(12)):
                if policy.step(candidate, winner, base=current):
                    winner = candidate
            if winner is current:
                break
            current, commits = winner, commits + 1
        if commits:
            assert policy.core(current, start), seed
        measured = policy.measure(
            current.quality_score["instructor_schedule"], current.lexicographic_score
        )
        assert measured.breaches == (0, 0, 0, 0), seed


def test_t16_the_runner_gate_is_never_stricter_than_the_strict_gates():
    """Without the day trade, a final board the strict per-instructor gates pass
    passes the policy's gate too; the policy also passes boards they block."""
    from core.services.timetable_v2_runner import (
        optimiser_instructor_day_regression,
        optimiser_instructor_idle_regression,
    )

    looser = 0
    for seed in range(500):
        rng = random.Random(seed)
        teachers = [str(i) for i in range(1, rng.randint(1, 4) + 1)]
        days0 = {t: rng.randint(1, 5) for t in teachers}
        idle0 = {t: rng.choice((0, 30, 90, 200)) for t in teachers}
        if rng.random() < 0.5:  # within every strict ceiling
            days1 = {t: rng.randint(1, days0[t]) for t in teachers}
            idle1 = {t: rng.randint(0, idle0[t]) for t in teachers}
        else:
            days1 = {t: rng.randint(1, 5) for t in teachers}
            idle1 = {t: rng.choice((0, 30, 90, 200)) for t in teachers}
        before, after = schedule(days0, idle0), schedule(days1, idle1)
        strict_blocked = (
            optimiser_instructor_day_regression(before, after, {})["blocked"]
            or optimiser_instructor_idle_regression(before, after, {})["blocked"]
        )
        score = (0, 0, 0, 0, rng.randint(0, 3), 0, 0, 0, 0)
        policy = policy_at(days0, idle0, (0, 0, 0, 0, 2, 0, 0, 0, 0))
        rows, _report = policy.final_check(after, score)
        if not strict_blocked:
            assert rows == [], seed
        elif not rows:
            looser += 1
    assert looser > 0  # R4 and R2 let through boards the strict gates block


# ── The flag, the scope and the untouched strict relation ─────────────────


def test_t17_the_strict_relation_is_unchanged_without_a_scope(settings):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = legacy_corpus()
    for name, priority in (("priority_on", True), ("priority_off", False)):
        settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = priority
        assert current_day_off_policy() is None
        assert legacy_outputs(policy_module, cases) == golden[name], name
        with day_off_scope(None):
            assert legacy_outputs(policy_module, cases) == golden[name], name


def test_the_policy_is_off_by_default_and_needs_instructor_priority(settings):
    from config import settings as project_settings

    assert project_settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY is False
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = False
    assert not is_day_off_policy_active()
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    assert is_day_off_policy_active()
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    assert not is_day_off_policy_active()


@override_settings(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
def test_the_scope_routes_evaluation_is_better_and_restores_the_strict_relation():
    """Inside a scope one instructor's idle may rise when the total falls (R4);
    outside, the strict per-instructor veto refuses the same move."""
    policy = policy_at({"1": 2, "2": 2}, {"1": 100, "2": 0})
    before = start_board(policy)
    after = board({"1": 2, "2": 2}, {"1": 0, "2": 30})
    assert not evaluation_is_better(after, before)
    with day_off_scope(policy):
        assert current_day_off_policy() is policy
        assert evaluation_is_better(after, before)
        with day_off_scope(None):
            assert not evaluation_is_better(after, before)
    assert current_day_off_policy() is None
    assert evaluation_order_key(after) == (0, 0, 0, 10, 4, 30, 1000, 5, 0, 0, 0)


def test_the_policy_round_trips_through_a_result_and_rejects_a_damaged_one():
    policy = policy_at({"1": 2, "2": 4}, {"1": 30, "2": 0}, saved_idle=255)
    stored = json.loads(json.dumps(policy.to_dict()))
    assert DayOffPolicy.from_dict(stored) == policy
    assert DayOffPolicy.from_dict({**stored, "audit": {}}) == policy
    for damaged in (
        None,
        {**stored, "version": 2},
        {**stored, "score": [0, True]},
        {**stored, "anchor": {**stored["anchor"], "total_idle": 31}},
        {**stored, "anchor": {**stored["anchor"], "padded": "yes"}},
        {k: v for k, v in stored.items() if k != "source"},
    ):
        assert DayOffPolicy.from_dict(damaged) is None


def test_swap_bounds_at_the_start_are_the_run_start_week():
    """At A the swap bounds are each teacher's saved week; the day trade's
    allowance applies only to a teacher who still has no day off."""
    policy = policy_at({"1": 5, "2": 3, "3": 0}, {"1": 40, "2": 10, "3": 0}, saved_idle=255)
    start = schedule(policy.anchor.days, policy.anchor.idle)
    assert policy.swap_bounds(start) == {"1": (5, 40, 255), "2": (3, 10, 0), "3": (0, 0, 0)}
    assert policy.anchor.start_bounds() == policy.swap_bounds(start)
    # A spare day given up earlier keeps its R3 minutes; a first day off keeps 255.
    later = schedule({"1": 4, "2": 4, "3": 0}, {"1": 40, "2": 50, "3": 0})
    assert policy.swap_bounds(later) == {"1": (4, 295, 0), "2": (4, 70, 0), "3": (0, 0, 0)}
