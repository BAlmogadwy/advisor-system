"""Owner ruling, 2026-11-22: fixing a teaching-time rule violation may cost a
teacher an extra working day (never the last day off). A violation stays a
warning, never a publish blocker.

``repair_instructor_availability`` chooses in two tiers: tier 0 is the cells the
instructor-week gate allows (chosen exactly as before); tier 1, used only when
tier 0 is empty, is the refused cells that cost no last day off, cheapest first
(fewest working days added, then the smallest idle rise). The run's final gates
credit what the repair spent to that teacher's baseline, per teacher, and a rise
beyond the credit blocks as before.

The direct-call tests open ``saved_week_scope`` (the Optimise Current promise to
keep the saved week), which makes the gate refuse every rise in a teacher's days
or idle, so tier 0 is empty unless a test builds a cell that raises nothing.
"""

from __future__ import annotations

import pytest

from core.models import SectionPlacement
from core.services import timetable_fixed_pattern_search as fps
from core.services import timetable_instructor_cap_repair as cap_repair
from core.services import timetable_v2_runner as runner
from core.services.instructor_availability import KIND_UNAVAILABLE
from core.services.timetable_course_policy import (
    CourseMeetingPolicy,
    WeeklyMeeting,
    WeeklyPattern,
)
from core.services.timetable_optimisation_policy import (
    DayOffAnchor,
    DayOffPolicy,
    instructor_spend,
    saved_week_scope,
    spend_order_key,
)
from tests._instructor_availability_fixture import ALL_DAYS
from tests.test_instructor_availability_engine import _av, _dr, _inject
from tests.test_instructor_availability_repair import (
    AM,
    LATE_AM,
    REPAIR_FLAGS,
    _cells,
    _run,
    _wed_pair_board,
    _x_cells,
    run_flags,  # noqa: F401  (a fixture, requested by name in ``gate``)
)
from tests.test_section_day_run import _SLOTS, _instructor_board


@pytest.fixture
def repair_flags(settings):
    for name, value in REPAIR_FLAGS.items():
        setattr(settings, name, value)
    return settings


def _open_only(*cells: tuple[str, str]) -> list[tuple[str, dict]]:
    """Every (day, slot) of the grid blocked except ``cells`` (day, start)."""
    keep = set(cells)
    return [(day, slot) for day in ALL_DAYS for slot in _SLOTS if (day, slot["start"]) not in keep]


def _no_wed_morning(person: int):
    """Dr Run may not teach on WED from 09:00 to 10:15 (C1's cell)."""
    return _av((person, KIND_UNAVAILABLE, ("WED",), 9 * 60, 10 * 60 + 15))


def _wed_board(name: str, extra, *cells: tuple[str, str]):
    """Dr Run teaches C5 (WED 10:30, legal) and C1 (WED 09:00, forbidden), plus
    ``extra`` sessions; only ``cells`` are open destinations. Leaving WED 09:00
    frees 15 idle minutes (the gap between C1 and C5) and no day."""
    sessions = [("C5", "S1", [("WED", *LATE_AM)]), *extra, ("C1", "S1", [("WED", *AM)])]
    return _instructor_board(name, sessions, blocked=_open_only(*cells))


def _repair_in_saved_week(scenario_id: int) -> dict:
    with saved_week_scope():
        return cap_repair.repair_instructor_availability(scenario_id)


# ── tier 0 and tier 1 ────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
def test_a_refused_new_day_is_spent_when_nothing_allowed_exists(repair_flags, monkeypatch) -> None:
    """Dr Run's only legal cell is MON 09:00, a day he does not work: his days rise
    1 -> 2, which the saved-week gate refuses. Nothing is allowed, so tier 1 takes
    it; the row carries the cost and the report the total spent."""
    scenario = _wed_board("sp tier1", [], ("MON", "09:00"))
    _inject(monkeypatch, _no_wed_morning(_dr()))

    report = _repair_in_saved_week(scenario.id)

    dr = str(_dr())
    assert _cells(scenario, "C1") == [("MON", "09:00")]
    assert report["locked_blocked"] == [] and report["remaining_violations"] == 0
    assert [row["cost"] for row in report["repaired"]] == [
        {"extra_days": {dr: 1}, "idle_minutes": {}}
    ]
    assert report["spent"] == {"extra_days": {dr: 1}, "idle_minutes": {}}


@pytest.mark.django_db(transaction=True)
def test_without_the_saved_week_the_same_move_is_tier_0_and_has_no_cost_row(
    repair_flags, monkeypatch
) -> None:
    """Control: outside the saved week the gate allows a spare day, so MON 09:00 is a
    tier-0 cell: the same move, no ``cost`` on the row (the total spent is still
    measured)."""
    scenario = _wed_board("sp tier0 control", [], ("MON", "09:00"))
    _inject(monkeypatch, _no_wed_morning(_dr()))

    report = cap_repair.repair_instructor_availability(scenario.id)

    assert _cells(scenario, "C1") == [("MON", "09:00")]
    assert "cost" not in report["repaired"][0]
    assert report["spent"]["extra_days"] == {str(_dr()): 1}


@pytest.mark.django_db(transaction=True)
def test_tier_1_is_not_used_while_a_cell_the_gate_allows_exists(repair_flags, monkeypatch) -> None:
    """TUE 10:30 (a day he works, the idle just moves from WED to TUE) raises nothing, so
    it is allowed; SUN 13:00 (a day he works too, earlier in the week) raises idle by 150
    and is refused. Tier 0 wins, chosen as before: no ``cost``, nothing spent."""
    scenario = _wed_board(
        "sp tier0 wins",
        [("C2", "S1", [("SUN", *AM)]), ("C4", "S1", [("TUE", *AM)])],
        ("SUN", "13:00"),
        ("TUE", "10:30"),
    )
    _inject(monkeypatch, _no_wed_morning(_dr()))

    report = _repair_in_saved_week(scenario.id)

    assert _cells(scenario, "C1") == [("TUE", "10:30")]
    assert "cost" not in report["repaired"][0] and "spent" not in report


def _last_day_off_board(name: str, *, spare_day: bool):
    """Dr Run works SUN, MON, TUE (C2, C3, C4) and WED; the only open cell is THU 09:00,
    which would be his fifth day (his fourth when ``spare_day``: no C4)."""
    extra = [("C2", "S1", [("SUN", *AM)]), ("C3", "S1", [("MON", *AM)])]
    if not spare_day:
        extra.append(("C4", "S1", [("TUE", *AM)]))
    return _wed_board(name, extra, ("THU", "09:00"))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("spare_day", [False, True], ids=["last-day-off", "spare-day-control"])
def test_the_last_day_off_is_never_spent(repair_flags, monkeypatch, spare_day) -> None:
    """The fifth working day stays forbidden: the meeting stays, is reported with
    ``needs_last_day_off`` and nothing is spent. Control: a fourth day is spent."""
    scenario = _last_day_off_board(f"sp last day {spare_day}", spare_day=spare_day)
    _inject(monkeypatch, _no_wed_morning(_dr()))

    report = _repair_in_saved_week(scenario.id)

    if spare_day:
        assert _cells(scenario, "C1") == [("THU", "09:00")]
        assert report["repaired"][0]["cost"]["extra_days"] == {str(_dr()): 1}
        return
    assert _cells(scenario, "C1") == [("WED", "09:00")]
    assert [row["reason"] for row in report["locked_blocked"]] == ["needs_last_day_off"]
    assert report["repaired"] == [] and "spent" not in report


@pytest.mark.django_db(transaction=True)
def test_tier_1_takes_the_smaller_idle_rise_not_the_earlier_day(repair_flags, monkeypatch) -> None:
    """Both open cells are on days he works, so both add no day: MON 14:30 raises his
    idle by 240, TUE 13:00 by 150. MON is earlier in the week; the cheaper TUE wins."""
    scenario = _wed_board(
        "sp idle order",
        [("C3", "S1", [("MON", *AM)]), ("C4", "S1", [("TUE", *AM)])],
        ("MON", "14:30"),
        ("TUE", "13:00"),
    )
    _inject(monkeypatch, _no_wed_morning(_dr()))

    report = _repair_in_saved_week(scenario.id)

    assert _cells(scenario, "C1") == [("TUE", "13:00")]
    assert report["repaired"][0]["cost"] == {"extra_days": {}, "idle_minutes": {str(_dr()): 150}}


@pytest.mark.django_db(transaction=True)
def test_tier_1_prefers_the_fewest_days_over_the_least_idle(repair_flags, monkeypatch) -> None:
    """MON 14:30 (a worked day, idle +240, no new day) against THU 09:00 (a new day,
    +1 day, idle falls): fewest days first, so MON."""
    scenario = _wed_board(
        "sp days first",
        [("C3", "S1", [("MON", *AM)])],
        ("MON", "14:30"),
        ("THU", "09:00"),
    )
    _inject(monkeypatch, _no_wed_morning(_dr()))

    report = _repair_in_saved_week(scenario.id)

    assert _cells(scenario, "C1") == [("MON", "14:30")]
    assert report["repaired"][0]["cost"]["extra_days"] == {}


def test_the_spend_of_a_change_is_the_per_teacher_rise_only() -> None:
    """A fall is not netted against another teacher's rise; a teacher the before side
    does not mention reads 0; the order key is (days, idle)."""
    before = {
        "working_days_by_instructor": {"1": 2, "2": 3},
        "idle_minutes_by_instructor": {"1": 30, "2": 0},
    }
    after = {
        "working_days_by_instructor": {"1": 3, "2": 2, "3": 1},
        "idle_minutes_by_instructor": {"1": 0, "2": 45, "3": 0},
    }
    spent = instructor_spend(before, after)
    assert spent == {"extra_days": {"1": 1, "3": 1}, "idle_minutes": {"2": 45}}
    assert spend_order_key(spent) == (2, 45)
    assert instructor_spend(before, before) == {"extra_days": {}, "idle_minutes": {}}


# ── the bundle path ──────────────────────────────────────────────────────


def _english_board(name: str):
    """ENG101 meets SUN, MON, WED, THU 09:00-12:00; Dr Run may not teach 09:00-11:00 on
    any day, and also teaches C2 on MON 12:00-12:45 (no gap after the morning bundle, a
    15 minute gap before an afternoon one)."""
    return _instructor_board(
        name,
        [
            ("ENG101", "S1", [(day, "09:00", "12:00") for day in ("SUN", "MON", "WED", "THU")]),
            ("C2", "S1", [("MON", "12:00", "12:45")]),
        ],
    )


def _no_mornings_until_11(person: int):
    return _av((person, KIND_UNAVAILABLE, ALL_DAYS, 9 * 60, 11 * 60))


def _bundle_policy(*patterns: tuple[str, tuple[str, ...], int, int]) -> CourseMeetingPolicy:
    return CourseMeetingPolicy(
        policy_id="TEST_BUNDLES",
        patterns=tuple(
            WeeklyPattern(
                pattern_id=f"TEST_BUNDLES_{name}",
                meetings=tuple(WeeklyMeeting(day, start, end) for day in days),
            )
            for name, days, start, end in patterns
        ),
    )


@pytest.mark.django_db(transaction=True)
def test_a_refused_bundle_is_spent_and_its_cost_reaches_the_report(
    repair_flags, monkeypatch
) -> None:
    """The afternoon bundle adds 15 idle minutes on MON, which the saved-week gate
    refuses; no other bundle exists, so tier 1 switches it. The bundle pass runs before
    the per-meeting loop measures its start, so the total spent comes from the bundle's
    own before/after."""
    scenario = _english_board("sp bundle")
    _inject(monkeypatch, _no_mornings_until_11(_dr()))

    report = _repair_in_saved_week(scenario.id)

    dr = str(_dr())
    starts = set(
        SectionPlacement.objects.filter(
            board__scenario=scenario, term_section__course_code="ENG101"
        ).values_list("start_time", flat=True)
    )
    assert starts == {"13:00"}
    assert report["weekly_pattern_search"]["accepted"] == 1
    assert report["weekly_pattern_search"]["spent_switches"] == [
        {"section_id": "ENG101_S1", "cost": {"extra_days": {}, "idle_minutes": {dr: 15}}}
    ]
    assert report["detected"] == [] and report["spent"] == {
        "extra_days": {},
        "idle_minutes": {dr: 15},
    }


@pytest.mark.django_db(transaction=True)
def test_the_total_spent_counts_the_bundle_pass_when_a_meeting_is_repaired_too(
    repair_flags, monkeypatch
) -> None:
    """The bundle pass spends 15 idle minutes (see above) and a separate meeting (C1,
    TUE 09:00, forbidden) then moves to TUE 13:00 at no cost. The per-meeting loop starts
    measuring only after the bundle pass, so the total must start from the bundle's own
    before: 15 minutes, not 0."""
    scenario = _instructor_board(
        "sp bundle and meeting",
        [
            ("ENG101", "S1", [(day, "09:00", "12:00") for day in ("SUN", "MON", "WED", "THU")]),
            ("C2", "S1", [("MON", "12:00", "12:45")]),
            ("C1", "S1", [("TUE", *AM)]),
        ],
        # The afternoon bundle's own cells stay open (13:00 and 14:30 on its four
        # days); C1's only free one is TUE 13:00 (the bundle overlaps the others).
        blocked=_open_only(
            ("TUE", "13:00"),
            *((day, start) for day in ("SUN", "MON", "WED", "THU") for start in ("13:00", "14:30")),
        ),
    )
    _inject(monkeypatch, _no_mornings_until_11(_dr()))

    report = _repair_in_saved_week(scenario.id)

    assert report["weekly_pattern_search"]["accepted"] == 1
    assert _cells(scenario, "C1") == [("TUE", "13:00")]
    assert [row["section"] for row in report["repaired"]] == ["C1 S1"]
    assert "cost" not in report["repaired"][0]
    assert report["spent"] == {"extra_days": {}, "idle_minutes": {str(_dr()): 15}}


@pytest.mark.django_db(transaction=True)
def test_a_bundle_the_gate_allows_beats_a_cheaper_looking_refused_one(
    repair_flags, monkeypatch
) -> None:
    """Tier 0 for bundles: C2 ends MON 13:00, so the afternoon bundle adds no idle (it even
    closes a 15 minute gap) and is allowed; the late one listed before it is refused
    (+idle). The allowed one is taken and the switch carries no cost."""
    scenario = _instructor_board(
        "sp bundle tier0",
        [
            ("ENG101", "S1", [(day, "09:00", "12:00") for day in ("SUN", "MON", "WED", "THU")]),
            ("C2", "S1", [("MON", "12:15", "13:00")]),
        ],
    )
    _inject(monkeypatch, _no_mornings_until_11(_dr()))
    days = ("SUN", "MON", "WED", "THU")
    ordered = _bundle_policy(
        ("AM", days, 540, 720), ("LATE", days, 960, 1140), ("PM", days, 780, 960)
    )
    monkeypatch.setattr(fps, "get_course_meeting_policy", lambda *_a, **_k: ordered)

    with saved_week_scope():
        result = fps.improve_fixed_course_patterns(scenario.id, repair_availability=True)

    assert result["accepted"] == 1 and "spent_switches" not in result
    starts = set(
        SectionPlacement.objects.filter(
            board__scenario=scenario, term_section__course_code="ENG101"
        ).values_list("start_time", flat=True)
    )
    assert starts == {"13:00"}


@pytest.mark.django_db(transaction=True)
def test_a_bundle_stays_when_every_legal_one_costs_the_last_day_off(
    repair_flags, monkeypatch
) -> None:
    """The only legal bundle meets on five days; Dr Run works four, so it would take his
    last day off and is never taken."""
    scenario = _english_board("sp bundle five")
    _inject(monkeypatch, _no_mornings_until_11(_dr()))
    five = _bundle_policy(
        ("AM", ("SUN", "MON", "WED", "THU"), 540, 720),
        ("PM5", ALL_DAYS, 780, 960),
    )
    monkeypatch.setattr(fps, "get_course_meeting_policy", lambda *_a, **_k: five)

    with saved_week_scope():
        result = fps.improve_fixed_course_patterns(scenario.id, repair_availability=True)

    assert result["accepted"] == 0
    starts = set(
        SectionPlacement.objects.filter(
            board__scenario=scenario, term_section__course_code="ENG101"
        ).values_list("start_time", flat=True)
    )
    assert starts == {"09:00"}


@pytest.mark.django_db(transaction=True)
def test_a_bundle_takes_the_cheapest_refused_one_not_the_first(repair_flags, monkeypatch) -> None:
    """Two legal bundles, both refused by the saved-week gate: the late one is listed
    first and costs 195 idle minutes on MON, the afternoon one 15. The cheaper wins."""
    scenario = _english_board("sp bundle order")
    _inject(monkeypatch, _no_mornings_until_11(_dr()))
    days = ("SUN", "MON", "WED", "THU")
    ordered = _bundle_policy(
        ("AM", days, 540, 720), ("LATE", days, 960, 1140), ("PM", days, 780, 960)
    )
    monkeypatch.setattr(fps, "get_course_meeting_policy", lambda *_a, **_k: ordered)

    with saved_week_scope():
        result = fps.improve_fixed_course_patterns(scenario.id, repair_availability=True)

    assert result["accepted"] == 1
    starts = set(
        SectionPlacement.objects.filter(
            board__scenario=scenario, term_section__course_code="ENG101"
        ).values_list("start_time", flat=True)
    )
    assert starts == {"13:00"}
    assert result["spent_switches"][0]["cost"]["idle_minutes"] == {str(_dr()): 15}


# ── the credit in the final gates (units) ────────────────────────────────


def _week(days: dict, idle: dict) -> dict:
    return {
        "working_days_by_instructor": days,
        "idle_minutes_by_instructor": idle,
        "working_days": sum(days.values()),
        "idle_minutes": sum(idle.values()),
        "affected_instructors": len(days),
    }


def _spent(days: dict | None = None, idle: dict | None = None) -> dict:
    return {
        "instructor_availability_repair": {
            "spent": {"extra_days": days or {}, "idle_minutes": idle or {}}
        }
    }


def _blocking(before: dict, after: dict, result: dict) -> list[tuple[str, str | None]]:
    """(metric, instructor) of every row either strict gate blocks or excepts."""
    day = runner.optimiser_instructor_day_regression(before, after, result)
    idle = runner.optimiser_instructor_idle_regression(before, after, result)
    return [
        (row["metric"], row.get("instructor_id"))
        for gate in (day, idle)
        for row in gate["regressions"] + gate["repair_exceptions"]
    ]


def test_a_day_the_repair_spent_is_credited_and_a_larger_rise_is_not() -> None:
    before = _week({"7": 2, "8": 2}, {"7": 0, "8": 0})
    one_more = _week({"7": 3, "8": 2}, {"7": 0, "8": 0})
    two_more = _week({"7": 4, "8": 2}, {"7": 0, "8": 0})
    other_teacher = _week({"7": 2, "8": 3}, {"7": 0, "8": 0})

    assert _blocking(before, one_more, {}) == [("instructor_working_days", "7")]
    assert _blocking(before, one_more, _spent({"7": 1})) == []
    assert _blocking(before, two_more, _spent({"7": 1})) == [("instructor_working_days", "7")]
    assert _blocking(before, two_more, _spent({"7": 2})) == []
    # The credit belongs to the teacher it was spent on.
    assert _blocking(before, other_teacher, _spent({"7": 1})) == [("instructor_working_days", "8")]


def test_a_fifth_working_day_is_never_credited() -> None:
    before = _week({"7": 4}, {"7": 0})
    after = _week({"7": 5}, {"7": 0})

    assert _blocking(before, after, _spent({"7": 1})) == [("instructor_working_days", "7")]
    assert _blocking(_week({"7": 3}, {"7": 0}), after, _spent({"7": 2})) == [
        ("instructor_working_days", "7")
    ]


def test_idle_minutes_the_repair_spent_are_credited_per_teacher_and_in_total() -> None:
    before = _week({"7": 2, "8": 2}, {"7": 30, "8": 0})
    after = _week({"7": 2, "8": 2}, {"7": 60, "8": 0})
    more = _week({"7": 2, "8": 2}, {"7": 61, "8": 0})
    elsewhere = _week({"7": 2, "8": 2}, {"7": 30, "8": 30})

    assert _blocking(before, after, _spent(idle={"7": 30})) == []
    assert _blocking(before, more, _spent(idle={"7": 30})) != []
    assert _blocking(before, after, {}) != []
    # The credit is that teacher's: another teacher's idle rise still blocks.
    assert ("instructor_idle_minutes", "8") in _blocking(before, elsewhere, _spent(idle={"7": 30}))


def test_a_run_that_spent_nothing_is_judged_exactly_as_before() -> None:
    before = _week({"7": 2}, {"7": 30})
    after = _week({"7": 3}, {"7": 45})
    plain = {
        "instructor_schedule_after_hard_repairs": after,
        "instructor_schedule_before_passes": before,
    }
    empty = {**plain, **_spent()}
    nothing = {**plain, "instructor_availability_repair": {"enabled": True}}

    for result in (empty, nothing):
        assert runner.optimiser_instructor_day_regression(
            before, after, result
        ) == runner.optimiser_instructor_day_regression(before, after, plain)
        assert runner.optimiser_instructor_idle_regression(
            before, after, result
        ) == runner.optimiser_instructor_idle_regression(before, after, plain)
    assert runner.availability_repair_credit(empty) == {"extra_days": {}, "idle_minutes": {}}
    # Only positive whole numbers are credit.
    assert runner.availability_repair_credit(
        _spent({"7": True, "8": -1, "9": 0, "10": 2}, {"7": "5", "8": 3})
    ) == {"extra_days": {"10": 2}, "idle_minutes": {"8": 3}}


def _policy(days: dict, idle: dict) -> DayOffPolicy:
    score = (0, 0, 0, 0, 0, 0)
    anchor = DayOffAnchor.from_metrics(_week(days, idle), padded=False)
    assert anchor is not None
    return DayOffPolicy(anchor=anchor, score=score, source="test")


def _check(policy: DayOffPolicy, days: dict, idle: dict) -> list[str]:
    rows, _report = policy.final_check(_week(days, idle), (0, 0, 0, 0, 0, 0))
    return [row["metric"] for row in rows]


def test_the_day_off_policy_credits_what_the_repair_spent_to_the_baseline() -> None:
    policy = _policy({"7": 2}, {"7": 30})
    credited = policy.with_credit({"7": 1}, {"7": 30})

    assert _check(policy, {"7": 3}, {"7": 60}) != []
    assert _check(credited, {"7": 3}, {"7": 60}) == []
    # A rise beyond the credit blocks exactly as the policy always did.
    assert _check(credited, {"7": 4}, {"7": 60}) != []
    assert _check(credited, {"7": 3}, {"7": 61}) != []
    # The policy itself is not changed by crediting.
    assert policy.anchor.days == {"7": 2} and policy.anchor.total_idle == 30
    assert credited.anchor.days == {"7": 3} and credited.anchor.total_idle == 60


def test_the_credit_used_is_bounded_by_the_rise_and_never_a_fifth_day() -> None:
    credit = {"extra_days": {"7": 2, "8": 1, "9": 3}, "idle_minutes": {"7": 40}}
    before = _week({"7": 2, "8": 4, "9": 1}, {"7": 10, "8": 0, "9": 0})
    after = _week({"7": 3, "8": 5, "9": 1}, {"7": 30, "8": 0, "9": 0})

    used = runner._availability_credit_used(before, after, credit)

    assert [(r["metric"], r["instructor_id"], r["used"]) for r in used] == [
        ("instructor_working_days", "7", 1),  # the rise (1), not the credit (2)
        ("instructor_idle_minutes", "7", 20),  # the rise (20), not the credit (40)
    ]  # teacher 8's fifth day and teacher 9's no rise are not credited


def test_the_day_off_policy_never_credits_a_fifth_day() -> None:
    policy = _policy({"7": 4, "8": 3}, {"7": 0, "8": 0})
    credited = policy.with_credit({"7": 1, "8": 5}, {})

    assert credited.anchor.days == {"7": 4, "8": 4}  # capped at MAX_WORKING_DAYS
    assert "instructor_fifth_working_day" in _check(credited, {"7": 5, "8": 4}, {"7": 0, "8": 0})
    assert _check(credited, {"7": 4, "8": 4}, {"7": 0, "8": 0}) == []
    # A teacher the baseline does not know gets nothing.
    assert policy.with_credit({"99": 1}, {"99": 5}).anchor == policy.anchor


# ── end to end through the guarded run ───────────────────────────────────


@pytest.fixture(params=[True, False], ids=["day-off-policy", "saved-week"])
def gate(request):
    """Optimise Current with the instructor priority on, once judged by the day-off
    policy and once by the strict saved-week gates."""
    flags = request.getfixturevalue("run_flags")
    flags.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = request.param
    return request.param


@pytest.mark.django_db
def test_a_run_keeps_the_day_its_repair_spent(gate) -> None:
    """Dr A teaches X on WED 09:00 (forbidden) and Y on WED 13:00 (legal); only a new day
    takes X off WED. The repair moves it, Dr A works one more day, the run is KEPT and the
    credit is reported."""
    sc, x, a = _wed_pair_board(f"e2e keep {gate}")

    result = _run(sc.id)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    assert _x_cells(sc, x)[0][0] != "WED"
    repair = result["instructor_availability_repair"]
    assert repair["locked_blocked"] == [] and not repair.get("rolled_back")
    assert repair["spent"]["extra_days"] == {str(a.pk): 1}
    assert repair["repaired"][0]["cost"]["extra_days"] == {str(a.pk): 1}
    metric = result["instructor_schedule_metric"]
    assert metric["after"]["working_days"] == metric["before"]["working_days"] + 1
    used = metric["availability_repair_exceptions"]
    assert [(row["metric"], row["instructor_id"], row["used"]) for row in used] == [
        ("instructor_working_days", str(a.pk), 1)
    ]
    assert ("availability_repair_credit" in metric.get("day_off_policy", {})) is gate
    assert result["instructor_availability_report"]["count"] == 0


@pytest.mark.django_db
def test_without_the_credit_the_same_run_is_blocked_and_rolled_back(gate, monkeypatch) -> None:
    """The credit is what keeps the run: with it removed the final gate sees an
    ordinary extra day and rolls the whole board back (the policy's spare-day breach,
    or the strict gates' baseline row)."""
    monkeypatch.setattr(
        runner,
        "availability_repair_credit",
        lambda _result: {"extra_days": {}, "idle_minutes": {}},
    )
    sc, x, _a = _wed_pair_board(f"e2e no credit {gate}")

    result = _run(sc.id)

    assert result["safety_blocked"] is True
    assert _x_cells(sc, x) == [("WED", "09:00")]


@pytest.mark.django_db
def test_a_rolled_back_run_reports_no_credit_used(gate, monkeypatch) -> None:
    """Another gate rolls the credited run back: the board is the saved one again, so the
    result must not claim the credit was used."""
    monkeypatch.setattr(
        runner,
        "optimiser_student_outcome_regression",
        lambda _result: {
            "blocked": True,
            "regressions": [{"metric": "forced", "label": "forced", "before": 0, "after": 1}],
        },
    )
    sc, x, _a = _wed_pair_board(f"e2e rollback {gate}")

    result = _run(sc.id)

    assert result["safety_blocked"] is True
    assert _x_cells(sc, x) == [("WED", "09:00")]
    assert "availability_repair_exceptions" not in result["instructor_schedule_metric"]
    assert result["instructor_availability_repair"]["rolled_back"] is True


@pytest.mark.django_db
def test_a_run_whose_repair_spent_nothing_reports_no_credit(gate) -> None:
    """Control: with a cell on SUN (a day Dr A already works) the repair spends nothing,
    and the result carries no ``spent``, no credit and no day-off credit key."""
    sc, x, _a = _wed_pair_board(f"e2e free {gate}", other_day="SUN")

    result = _run(sc.id)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    assert _x_cells(sc, x)[0][0] == "SUN"
    assert "spent" not in result["instructor_availability_repair"]
    metric = result["instructor_schedule_metric"]
    assert "availability_repair_exceptions" not in metric
    assert "availability_repair_credit" not in metric.get("day_off_policy", {})
