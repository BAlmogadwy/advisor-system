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
    """(metric, instructor) of every row either strict gate blocks or excepts. Unless the
    result says otherwise the repair left the board where this check finds it (``after``)."""
    result = {"instructor_schedule_after_hard_repairs": after, **result}
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
        lambda *_args: {"extra_days": {}, "idle_minutes": {}},
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


# ── the bundle report keeps its old keys ─────────────────────────────────


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("paid", [False, True], ids=["free-switch", "paid-switch"])
def test_the_bundle_report_gains_only_the_spent_switches_key(
    repair_flags, monkeypatch, paid
) -> None:
    """A switch that costs nothing leaves ``weekly_pattern_search`` exactly as it was
    before the change (``evaluated`` and ``accepted``); a paid one adds only
    ``spent_switches``. The measurements the repair needs are not left in the report."""
    if paid:
        scenario = _english_board("sp keys paid")
    else:
        scenario = _instructor_board(
            "sp keys free",
            [
                ("ENG101", "S1", [(d, "09:00", "12:00") for d in ("SUN", "MON", "WED", "THU")]),
                ("C2", "S1", [("MON", "12:15", "13:00")]),
            ],
        )
    _inject(monkeypatch, _no_mornings_until_11(_dr()))

    report = (
        _repair_in_saved_week(scenario.id)
        if paid
        else cap_repair.repair_instructor_availability(scenario.id)
    )

    keys = set(report["weekly_pattern_search"])
    assert keys == {"evaluated", "accepted"} | ({"spent_switches"} if paid else set())
    assert report["weekly_pattern_search"]["accepted"] == 1
    assert ("spent" in report) is paid


# ── the credit is only what the repair left owed ─────────────────────────


def _hard(days: int, idle: int = 0) -> dict:
    return _week({"1": days}, {"1": idle})


def test_a_day_an_earlier_stage_saved_and_the_repair_spent_back_is_owed_nothing() -> None:
    """Teacher 1: 3 days at the start, an earlier stage saves one (2), the repair spends
    it back (3). The board right after the repairs stands at 3, as the saved one, so
    the repair is owed no credit; a later stage's rise 3 -> 4 is then a plain rise."""
    saved = _week({"1": 3}, {"1": 0})
    four = _week({"1": 4}, {"1": 0})
    spent = _spent({"1": 1})

    def with_hard(days: int) -> dict:
        return {**spent, "instructor_schedule_after_hard_repairs": _hard(days)}

    assert runner.availability_repair_credit(with_hard(3), saved)["extra_days"] == {}
    assert runner.availability_repair_credit(with_hard(4), saved)["extra_days"] == {"1": 1}
    assert _blocking(saved, four, with_hard(3)) == [("instructor_working_days", "1")]
    assert _blocking(saved, four, with_hard(4)) == []
    # Idle is cut the same way.
    idle = {
        **_spent(idle={"1": 30}),
        "instructor_schedule_after_hard_repairs": _hard(3, 20),
    }
    credit = runner.availability_repair_credit(idle, _week({"1": 3}, {"1": 0}))
    assert credit["idle_minutes"] == {"1": 20}


# The runner's stand-in optimiser (see ``test_timetable_runner_day_off_policy``): it moves
# placements, reports the scores and what the repair "spent".

from core.services.timetable_optimisation_policy import (  # noqa: E402
    day_off_scope,
    week_move_breaks_gate,
)
from tests.test_timetable_runner_day_off_policy import (  # noqa: E402
    BASE,
    GAIN,
    SPARE_DAY,
    THREE_DAYS,
    WORSE,
    _board,
)


def _stand_in_run(
    monkeypatch,
    moves,
    *,
    final,
    before_passes,
    spent,
    hard_days,
    hard_idle=0,
    placements=THREE_DAYS,
    stage=None,
    bundle_only=False,
    late_moves=(),
):
    """``stage(scenario_id)`` runs inside the stand-in, after the moves, under the policy
    scope (a later repair stage) and returns what it adds to the result. ``hard_days``
    None leaves out the record of the board after the hard repairs; ``bundle_only``
    instead has the REAL optimiser helper record it for a repair that spent through a
    bundle switch (no ``repaired`` row), and ``late_moves`` are applied after that."""
    from core.services import timetable_optimizer_v2 as optimizer

    scenario, board = _board(placements)
    teacher_map = {f"{code}_S1": frozenset({1}) for code, *_rest in placements}
    monkeypatch.setattr(
        optimizer, "build_section_instructor_map_for_scenario", lambda _sid: teacher_map
    )
    monkeypatch.setattr(runner, "compute_scenario_safety_summary", lambda _sid: {})

    def move(rows):
        for code, day, start, end in rows:
            SectionPlacement.objects.filter(board=board, term_section__course_code=code).update(
                day=day, start_time=start, end_time=end
            )

    def optimise(**kwargs):
        move(moves)
        policy = DayOffPolicy(kwargs["day_off_anchor"], BASE, "saved_board")
        result = {
            "baseline_score": list(BASE),
            "final_score": list(final),
            "score_before_instructor_passes": list(before_passes),
            "day_off_policy": policy.to_dict(),
            "instructor_availability_repair": {"spent": spent},
        }
        if bundle_only:
            result["instructor_availability_repair"] = {
                "repaired": [],
                "spent": spent,
                "weekly_pattern_search": {"evaluated": 1, "accepted": 1},
            }
            optimizer.capture_instructor_schedule_after_hard_repairs(
                result, scenario.id, teacher_map
            )
            move(late_moves)
        elif hard_days is not None:
            result["instructor_schedule_after_hard_repairs"] = _hard(hard_days, hard_idle)
        if stage is not None:
            with day_off_scope(policy):
                result.update(stage(scenario.id))
        return result

    monkeypatch.setattr(optimizer, "optimise_current_timetable", optimise)
    return scenario, runner.run_v2_optimisation_guarded(scenario.id, mode="current")


@pytest.fixture
def policy_on(settings):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    return settings


@pytest.mark.django_db
def test_a_spent_back_day_does_not_become_a_false_saved_day_in_the_policy(
    policy_on, monkeypatch
) -> None:
    """The day-off policy's final check: the board ends where the saved one began (3
    days), the repair reported a spent day that an earlier stage had saved. The policy
    baseline stays 3: no ``days_before: 4``, no saved day, no credit key."""
    _sc, result = _stand_in_run(
        monkeypatch,
        [],
        final=BASE,
        before_passes=BASE,
        spent={"extra_days": {"1": 1}, "idle_minutes": {}},
        hard_days=3,
    )

    assert result["safety_blocked"] is False, result.get("safety_regression")
    metric = result["instructor_schedule_metric"]
    teacher = metric["day_off_policy"]["teachers"]["1"]
    assert (teacher["days_before"], teacher["days_after"], teacher["days_saved"]) == (3, 3, 0)
    assert "availability_repair_credit" not in metric["day_off_policy"]
    assert "availability_repair_exceptions" not in metric


@pytest.mark.django_db
@pytest.mark.parametrize("hard_days", [3, 4], ids=["spent-back-day", "day-still-owed-control"])
def test_a_later_stage_rise_is_blocked_unless_the_repair_still_owes_the_day(
    policy_on, monkeypatch, hard_days
) -> None:
    """A later stage gives teacher 1 a fourth day with no student gain. If the repair
    left the board at 3 days (it only spent back a saved day) nothing covers it and the
    run is blocked; if the repair itself ended at 4 days the credit covers one day and
    the run is kept."""
    _sc, result = _stand_in_run(
        monkeypatch,
        SPARE_DAY,
        final=BASE,
        before_passes=BASE,
        spent={"extra_days": {"1": 1}, "idle_minutes": {}},
        hard_days=hard_days,
    )

    blocked = hard_days == 3
    assert result["safety_blocked"] is blocked
    if blocked:
        assert [r["metric"] for r in result["safety_regression"]["regressions"]] == [
            "instructor_spare_day_without_student_gain"
        ]
    else:
        credit = result["instructor_schedule_metric"]["day_off_policy"][
            "availability_repair_credit"
        ]
        assert credit["extra_days"] == {"1": 1}


# ── a refused cell that only costs the students ──────────────────────────


def test_the_gate_refuses_a_move_that_only_voids_an_earlier_spare_days_student_gain() -> None:
    """The premise of the next test: teacher 7 gave up a spare day earlier in the run for
    a student gain (R2 holds). A move that changes none of his days or idle but worsens
    the score voids that gain, so the day-off policy's gate refuses it."""
    anchor = DayOffAnchor.from_metrics(_week({"7": 3}, {"7": 0}), padded=False)
    policy = DayOffPolicy(anchor=anchor, score=BASE, source="test")
    four_days = _week({"7": 4}, {"7": 0})

    with day_off_scope(policy):
        assert week_move_breaks_gate(four_days, GAIN, four_days, WORSE) is True
        assert week_move_breaks_gate(four_days, GAIN, four_days, GAIN) is False


_HOURLY = [{"start": f"{h:02d}:00", "end": f"{h + 1:02d}:00"} for h in (8, 9, 10, 11)]


@pytest.mark.django_db
def test_a_free_move_the_policy_gate_refuses_for_students_keeps_the_run(
    policy_on, monkeypatch
) -> None:
    """Teacher 1 gave up a spare day (G to WED) for a student gain. X (MON 10:00, forbidden)
    can move to MON 08:00, which changes none of his days or idle but worsens the score the
    repair sees, so the gate refuses it and tier 1 takes it at zero cost and zero credit.
    The final gate reads R2 on the optimiser's own board (``score_before_instructor_passes``,
    still the gain), so the spare day stays justified: the run is KEPT and X has moved,
    where before this change X stayed."""
    from types import SimpleNamespace

    from core.models import TimetableScenario
    from core.services import timetable_candidate_eval as candidate_eval
    from core.services import timetable_optimizer_v2 as optimizer

    monkeypatch.setattr(optimizer, "build_student_profiles_for_scenario", lambda _sid: {"s": 1})
    monkeypatch.setattr(optimizer, "build_course_rigidity_for_scenario", lambda _sid: {})

    def evaluate(_name, states, *_args, **_kwargs):
        at_ten = any(
            m.start_min == 600
            for state in states
            if state.section_id == "X_S1"
            for m in state.meetings
        )
        return SimpleNamespace(lexicographic_score=GAIN if at_ten else WORSE)

    monkeypatch.setattr(candidate_eval, "evaluate_generated_timetable_candidate", evaluate)
    _inject(monkeypatch, _av((1, KIND_UNAVAILABLE, ("MON",), 10 * 60, 11 * 60)))

    def repair(scenario_id: int) -> dict:
        TimetableScenario.objects.filter(pk=scenario_id).update(
            slot_config=_HOURLY,
            blocked_slots=[
                {"day": day, "start": slot["start"]}
                for day in ALL_DAYS
                for slot in _HOURLY
                if (day, slot["start"]) != ("MON", "08:00")
            ],
        )
        return {
            "instructor_availability_repair": cap_repair.repair_instructor_availability(scenario_id)
        }

    scenario, result = _stand_in_run(
        monkeypatch,
        SPARE_DAY,
        final=WORSE,
        before_passes=GAIN,
        spent={},
        hard_days=4,
        placements=[*THREE_DAYS, ("X", "MON", "10:00", "11:00")],
        stage=repair,
    )

    report = result["instructor_availability_repair"]
    assert [row["to"] for row in report["repaired"]] == ["MON 08:00-09:00"]
    assert report["repaired"][0]["cost"] == {"extra_days": {}, "idle_minutes": {}}
    assert "spent" not in report
    assert (
        SectionPlacement.objects.get(
            board__scenario=scenario, term_section__course_code="X"
        ).start_time
        == "08:00"
    )
    assert result["safety_blocked"] is False, result.get("safety_regression")
    policy = result["instructor_schedule_metric"]["day_off_policy"]
    assert policy["student_gain"] is True and "availability_repair_credit" not in policy
    assert policy["teachers"]["1"]["days_given_up"] == 1


@pytest.mark.django_db
def test_a_day_a_later_stage_saved_back_is_not_credited_either(policy_on, monkeypatch) -> None:
    """The repair ended at 4 days (it spent one), then a later stage brought teacher 1
    back to the saved 3: the final board owes nothing, so the policy baseline stays 3
    and the saved day is not shown as a false 4 -> 3."""
    _sc, result = _stand_in_run(
        monkeypatch,
        [],
        final=BASE,
        before_passes=BASE,
        spent={"extra_days": {"1": 1}, "idle_minutes": {}},
        hard_days=4,
    )

    assert result["safety_blocked"] is False, result.get("safety_regression")
    policy = result["instructor_schedule_metric"]["day_off_policy"]
    teacher = policy["teachers"]["1"]
    assert (teacher["days_before"], teacher["days_after"], teacher["days_saved"]) == (3, 3, 0)
    assert "availability_repair_credit" not in policy


@pytest.mark.django_db
def test_the_policy_baseline_rises_only_by_what_the_final_board_still_owes(
    policy_on, monkeypatch
) -> None:
    """The repair spent 30 idle minutes and ended 30 above the saved board, but a later
    stage left the final board only 10 above it: the baseline rises by 10, not 30."""
    _sc, result = _stand_in_run(
        monkeypatch,
        [("F", "SUN", "10:10", "11:10")],
        final=BASE,
        before_passes=BASE,
        spent={"extra_days": {}, "idle_minutes": {"1": 30}},
        hard_days=3,
        hard_idle=30,
    )

    assert result["safety_blocked"] is False, result.get("safety_regression")
    policy = result["instructor_schedule_metric"]["day_off_policy"]
    assert policy["availability_repair_credit"] == {"extra_days": {}, "idle_minutes": {"1": 10}}
    assert policy["teachers"]["1"]["idle_before"] == 10


# ── a repair that spent only through a bundle switch ─────────────────────

_ONE_DAY = {"extra_days": {"1": 1}, "idle_minutes": {}}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("early", "late", "blocked"),
    [
        (SPARE_DAY, (), False),  # the bundle switch left teacher 1 on 4 days: owed
        ((), SPARE_DAY, True),  # it left him on 3 (it spent back a saved day): a later rise
    ],
    ids=["bundle-owes-the-day", "bundle-spent-back-a-saved-day"],
)
def test_a_bundle_only_spend_is_clamped_like_any_other(
    policy_on, monkeypatch, early, late, blocked
) -> None:
    """The optimiser records the board after the hard repairs also when the repair's only
    work was a bundle switch (no ``repaired`` row) that spent: the credit is the day the
    board then stands above the saved one, so a later stage's rise only passes when the
    bundle really owes it."""
    _sc, result = _stand_in_run(
        monkeypatch,
        early,
        final=BASE,
        before_passes=BASE,
        spent=_ONE_DAY,
        hard_days=None,
        bundle_only=True,
        late_moves=late,
    )

    assert result["safety_blocked"] is blocked
    if blocked:
        assert [r["metric"] for r in result["safety_regression"]["regressions"]] == [
            "instructor_spare_day_without_student_gain"
        ]


@pytest.mark.django_db
def test_a_spend_without_the_record_of_the_repaired_board_is_never_credited(
    policy_on, monkeypatch
) -> None:
    """Defence in depth: if the record is missing nothing shows how far above the saved
    board the spend stands, so there is no credit and the rise blocks."""
    _sc, result = _stand_in_run(
        monkeypatch,
        SPARE_DAY,
        final=BASE,
        before_passes=BASE,
        spent=_ONE_DAY,
        hard_days=None,
    )

    assert result["safety_blocked"] is True


def test_the_credit_is_empty_for_a_spend_without_a_record_only_when_a_baseline_is_given() -> None:
    result = _spent({"1": 1})

    assert runner.availability_repair_credit(result, _week({"1": 3}, {"1": 0})) == {
        "extra_days": {},
        "idle_minutes": {},
    }
    assert runner.availability_repair_credit(result)["extra_days"] == {"1": 1}
