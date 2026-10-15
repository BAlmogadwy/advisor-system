"""Bounded instructor-day trade (default OFF).

The compaction pass normally refuses any move that raises an instructor's idle
minutes, so it can never save a campus day: merging two days always leaves a
gap. With ``TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED`` a move that saves a day may
spend a configured amount of idle (and, bounded, student gap); a day that holds
two sessions is emptied by moving both together (``..._VACATE_DAYS``).
"""

from __future__ import annotations

import pytest

_SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:30", "end": "11:45"},
    {"start": "13:00", "end": "14:15"},
    {"start": "14:30", "end": "15:45"},
]


def _board(placements):
    """One instructor teaching ``[(code, day, start, end), ...]`` (no students)."""
    from core.models import (
        CourseInstructor,
        DeliveryBoard,
        Instructor,
        Room,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )
    from core.services.course_instructor_assignment import apply_primary_instructor
    from core.services.timetable_pr4_instructor import normalise_instructor

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="AI M T1 day trade",
        gender="M",
        programs=["AI"],
        slot_config=_SLOTS,
        lab_slot_config=[],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    instr = Instructor.objects.create(
        full_name="Dr Days", normalised_name=normalise_instructor("Dr Days")
    )
    Room.objects.create(
        room_code="R1", department="AI", capacity=50, room_type="lecture", section="M"
    )
    for code, day, start, end in placements:
        CourseInstructor.objects.create(
            program="AI", course_code=code, section="M", instructor=instr, role="primary"
        )
        ts = TermSection.objects.create(
            scenario=scenario,
            course_key=code,
            section="S1",
            course_code=code,
            course_number=code,
            course_name=code,
            available_capacity=30,
            source_tag="day_trade_test",
        )
        TermSectionMeeting.objects.create(
            term_section=ts, day=day, start_time=start, end_time=end, room="", instructor=""
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            room="R1",
            is_locked=False,
        )
        apply_primary_instructor(ts, scenario, board, ts.course_code)
    return scenario


def _days_used(scenario) -> int:
    from core.models import SectionPlacement

    return len(
        set(
            SectionPlacement.objects.filter(board__scenario=scenario)
            .exclude(day="")
            .values_list("day", flat=True)
        )
    )


@pytest.fixture
def _compaction(settings):
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 6
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0


def test_trade_is_off_by_default():
    from core.services.timetable_pr4_instructor import get_instructor_day_trade_config

    cfg = get_instructor_day_trade_config()
    # Off by default; the other defaults are the recommended trial values, so the
    # single ENABLED flag is all that has to be switched on.
    assert cfg["enabled"] is False
    assert cfg["vacate_days"] is True
    assert cfg["idle_allowance"] == 150
    assert cfg["per_student"] == 500
    assert cfg["high_risk_per_student"] == 500
    assert cfg["total_gap"] == 300
    assert cfg["reserve_allowance"] == 0


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
def test_saving_a_day_costs_idle_so_default_compaction_never_does_it():
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    # MON 09:00 and TUE 09:00: any merge leaves a 15-minute hole.
    scenario = _board([("C1", "MON", "09:00", "10:15"), ("C2", "TUE", "09:00", "10:15")])
    report = compact_instructor_schedules(scenario.id)

    assert report["instructor_impact"]["working_days_after"] == 2
    assert _days_used(scenario) == 2
    assert report["trade_rule"]["enabled"] is False


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
def test_trade_saves_a_day_within_the_idle_allowance(settings):
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 15
    scenario = _board([("C1", "MON", "09:00", "10:15"), ("C2", "TUE", "09:00", "10:15")])

    report = compact_instructor_schedules(scenario.id)

    assert report["instructor_impact"]["working_days_before"] == 2
    assert report["instructor_impact"]["working_days_after"] == 1
    assert _days_used(scenario) == 1
    # Exactly the allowance was spent: the two sessions are 15 minutes apart.
    assert report["instructor_impact"]["total_idle_after"] == 15


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
def test_trade_refuses_when_the_idle_allowance_is_too_small(settings):
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 10  # the merge costs 15
    scenario = _board([("C1", "MON", "09:00", "10:15"), ("C2", "TUE", "09:00", "10:15")])

    report = compact_instructor_schedules(scenario.id)

    assert report["instructor_impact"]["working_days_after"] == 2
    assert _days_used(scenario) == 2


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
@pytest.mark.parametrize("vacate", [False, True], ids=["single-moves", "vacate-days"])
def test_a_two_session_day_needs_the_joint_vacate_move(settings, vacate):
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 150
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_VACATE_DAYS = vacate
    # Every day holds two sessions, so no day can be emptied one session at a
    # time (the first move leaves the day open). Emptying MON takes both of its
    # sessions at once, onto TUE and WED, which stay within the cap of three.
    scenario = _board(
        [
            ("A1", "MON", "09:00", "10:15"),
            ("A2", "MON", "10:30", "11:45"),
            ("B1", "TUE", "09:00", "10:15"),
            ("B2", "TUE", "10:30", "11:45"),
            ("C1", "WED", "09:00", "10:15"),
            ("C2", "WED", "10:30", "11:45"),
        ]
    )

    report = compact_instructor_schedules(scenario.id)

    assert report["instructor_impact"]["working_days_before"] == 3
    expected = 2 if vacate else 3
    assert report["instructor_impact"]["working_days_after"] == expected
    assert _days_used(scenario) == expected


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
def test_trade_never_moves_the_instructor_past_the_daily_cap(settings):
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 600
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_VACATE_DAYS = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = 2
    scenario = _board(
        [
            ("A1", "MON", "09:00", "10:15"),
            ("A2", "MON", "10:30", "11:45"),
            ("B1", "TUE", "09:00", "10:15"),
            ("B2", "TUE", "10:30", "11:45"),
        ]
    )

    compact_instructor_schedules(scenario.id)

    from collections import Counter

    from core.models import SectionPlacement

    per_day = Counter(
        SectionPlacement.objects.filter(board__scenario=scenario).values_list("day", flat=True)
    )
    assert max(per_day.values()) <= 2  # two full days cannot merge under a cap of two


def test_runner_tolerates_idle_only_for_days_actually_saved(settings):
    from core.services.timetable_v2_runner import optimiser_instructor_idle_regression

    def metrics(days, idle):
        return {
            "working_days": sum(days.values()),
            "working_days_by_instructor": days,
            "idle_minutes": sum(idle.values()),
            "idle_minutes_by_instructor": idle,
            "affected_instructors": len(days),
        }

    before = metrics({"1": 3, "2": 2}, {"1": 30, "2": 0})
    after = metrics({"1": 2, "2": 2}, {"1": 90, "2": 0})  # teacher 1 saved a day, +60 idle

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = False
    assert optimiser_instructor_idle_regression(before, after, {})["blocked"] is True

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 60
    gate = optimiser_instructor_idle_regression(before, after, {})
    assert gate["blocked"] is False
    assert {r["reason"] for r in gate["trade_exceptions"]} == {"instructor_day_saved_for_idle"}

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 59
    assert optimiser_instructor_idle_regression(before, after, {})["blocked"] is True

    # Teacher 2 saved nothing, so their idle gains stay forbidden even though the
    # aggregate allowance (teacher 1's 60) would cover it.
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 60
    worse = metrics({"1": 2, "2": 2}, {"1": 30, "2": 15})
    assert optimiser_instructor_idle_regression(before, worse, {})["blocked"] is True


# ---------------------------------------------------------------------------
# Student-gap bounds, tie-break and the rule-OFF safety property. These drive the
# real compaction pass with a stand-in student evaluator, so the gap each student
# pays for a given board is exactly what the test says it is.
# ---------------------------------------------------------------------------


def _install_students(monkeypatch, *, tiers, gap_of):
    """Replace the student profiles and the evaluator.

    ``tiers`` maps student id -> RiskTier; ``gap_of(student_id, states)`` is that
    student's total gap minutes on the board ``states`` (a list of SectionState).
    The returned evaluation carries the REAL instructor schedule metrics, so the
    instructor side of the policy behaves exactly as in production.
    """
    from types import SimpleNamespace

    from core.services import timetable_candidate_eval, timetable_optimizer_v2
    from core.services.timetable_assignment_models import (
        StudentAssignmentState,
        TimetableEvaluationResult,
    )
    from core.services.timetable_optimisation_policy import instructor_schedule_metrics

    profiles = {sid: SimpleNamespace(risk_tier=tier) for sid, tier in tiers.items()}
    monkeypatch.setattr(
        timetable_optimizer_v2, "build_student_profiles_for_scenario", lambda _sid: profiles
    )

    def evaluate(
        candidate_id, states, profs, rigidity, section_instructor_ids=None, course_tiers=None
    ):
        per = {
            sid: StudentAssignmentState(sid, total_gap_minutes=gap_of(sid, states)) for sid in profs
        }
        return TimetableEvaluationResult(
            candidate_id,
            (0, 0, 0, 0, 0, 0),  # student cost deliberately flat: ranking must not lean on it
            per,
            [],
            [],
            [],
            [],
            quality_score={
                "instructor_schedule": instructor_schedule_metrics(states, section_instructor_ids)
            },
        )

    monkeypatch.setattr(
        timetable_candidate_eval, "evaluate_generated_timetable_candidate", evaluate
    )


def _days(states) -> int:
    return len({m.day for s in states for m in s.meetings})


def _gap_when_a_day_is_saved(extra):
    """Every student pays ``extra[student]`` more once the board uses one day."""

    def gap_of(sid, states):
        return 1000 + (extra.get(sid, 0) if _days(states) == 1 else 0)

    return gap_of


def _saved_days(settings, monkeypatch, *, tiers, extra, per_student=500, high_risk=500, total=300):
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 150
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_PER_STUDENT = per_student
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_HIGH_RISK_PER_STUDENT = high_risk
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_TOTAL_GAP = total
    _install_students(monkeypatch, tiers=tiers, gap_of=_gap_when_a_day_is_saved(extra))
    scenario = _board([("C1", "MON", "09:00", "10:15"), ("C2", "TUE", "09:00", "10:15")])
    compact_instructor_schedules(scenario.id)
    return _days_used(scenario)


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
@pytest.mark.parametrize(("extra", "saved"), [(500, True), (501, False)])
def test_per_student_cap_is_exact(settings, monkeypatch, extra, saved):
    from core.services.timetable_assignment_models import RiskTier

    days = _saved_days(
        settings, monkeypatch, tiers={"S1": RiskTier.C}, extra={"S1": extra}, total=10**6
    )
    assert days == (1 if saved else 2)


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
@pytest.mark.parametrize(("second", "saved"), [(150, True), (151, False)])
def test_total_added_gap_cap_is_exact(settings, monkeypatch, second, saved):
    from core.services.timetable_assignment_models import RiskTier

    tiers = {"S1": RiskTier.C, "S2": RiskTier.C}
    days = _saved_days(settings, monkeypatch, tiers=tiers, extra={"S1": 150, "S2": second})
    assert days == (1 if saved else 2)  # 300 in total is allowed, 301 is not


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
@pytest.mark.parametrize("tier_name", ["A", "B"])
@pytest.mark.parametrize(("extra", "saved"), [(100, True), (101, False)])
def test_high_risk_and_graduating_cap_is_tighter_and_exact(
    settings, monkeypatch, tier_name, extra, saved
):
    from core.services.timetable_assignment_models import RiskTier

    tiers = {"S1": getattr(RiskTier, tier_name)}
    days = _saved_days(
        settings,
        monkeypatch,
        tiers=tiers,
        extra={"S1": extra},
        per_student=500,
        high_risk=100,
        total=10**6,
    )
    assert days == (1 if saved else 2)


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
def test_ordinary_students_may_use_the_wider_cap_while_high_risk_stay_protected(
    settings, monkeypatch
):
    from core.services.timetable_assignment_models import RiskTier

    tiers = {"S1": RiskTier.A, "S2": RiskTier.C}
    days = _saved_days(
        settings,
        monkeypatch,
        tiers=tiers,
        extra={"S2": 290},  # an ordinary student pays 290; the high-risk student pays nothing
        per_student=500,
        high_risk=0,
        total=10**6,
    )
    assert days == 1


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
def test_ties_on_instructor_idle_go_to_the_candidate_with_the_least_student_cost(
    settings, monkeypatch
):
    """Two moves cost the same idle; the one that hurts students less must win."""
    from core.models import SectionPlacement
    from core.services.timetable_assignment_models import RiskTier
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 150
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 1

    def gap_of(sid, states):
        # C2 TUE 09:00 -> MON 10:30 and C1 MON 09:00 -> TUE 10:30 both save a day for
        # the same 15 idle minutes. Moving C1 costs the students more.
        by_course = {s.course_code: s for s in states}
        c1_moved = by_course["C1"].meetings[0].start_min != 9 * 60
        return 1000 + (200 if _days(states) == 1 and c1_moved else 50 if _days(states) == 1 else 0)

    _install_students(monkeypatch, tiers={"S1": RiskTier.C}, gap_of=gap_of)
    scenario = _board([("C1", "MON", "09:00", "10:15"), ("C2", "TUE", "09:00", "10:15")])

    compact_instructor_schedules(scenario.id)

    c1 = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C1")
    c2 = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C2")
    assert (c1.day, c1.start_time) == ("MON", "09:00")  # the dearer move was not chosen
    assert c2.day == "MON"


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
def test_ties_on_total_gap_go_to_the_smaller_worst_student(settings, monkeypatch):
    from core.models import SectionPlacement
    from core.services.timetable_assignment_models import RiskTier
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 150
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 1

    def gap_of(sid, states):
        by_course = {s.course_code: s for s in states}
        c1_moved = by_course["C1"].meetings[0].start_min != 9 * 60
        if _days(states) != 1:
            return 1000
        # Same total (200) either way; moving C1 concentrates it on one student.
        if c1_moved:
            return 1000 + (200 if sid == "S1" else 0)
        return 1000 + 100

    tiers = {"S1": RiskTier.C, "S2": RiskTier.C}
    _install_students(monkeypatch, tiers=tiers, gap_of=gap_of)
    scenario = _board([("C1", "MON", "09:00", "10:15"), ("C2", "TUE", "09:00", "10:15")])

    compact_instructor_schedules(scenario.id)

    c1 = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C1")
    assert (c1.day, c1.start_time) == ("MON", "09:00")  # C2 moved; the tail is 100, not 200


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
def test_total_added_gap_outranks_the_worst_student_in_the_tie_break(settings, monkeypatch):
    """Lower TOTAL first, then lower worst student: not the other way round."""
    from core.models import SectionPlacement
    from core.services.timetable_assignment_models import RiskTier
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 150
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 1

    def gap_of(sid, states):
        by_course = {s.course_code: s for s in states}
        if _days(states) != 1:
            return 1000
        if by_course["C1"].meetings[0].start_min != 9 * 60:  # C1 moved: total 240, worst 120
            return 1000 + 120
        return 1000 + (200 if sid == "S1" else 0)  # C2 moved: total 200, worst 200

    tiers = {"S1": RiskTier.C, "S2": RiskTier.C}
    _install_students(monkeypatch, tiers=tiers, gap_of=gap_of)
    scenario = _board([("C1", "MON", "09:00", "10:15"), ("C2", "TUE", "09:00", "10:15")])

    compact_instructor_schedules(scenario.id)

    c1 = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C1")
    assert (c1.day, c1.start_time) == ("MON", "09:00")  # C2 moved: 200 in total beats 240


_ORIGINAL_SLOT = {"C1": (0, 540), "C2": (0, 870), "C3": (1, 630), "C4": (2, 540), "C5": (2, 870)}


def _hashed_gap(displacement_weight):
    """An arbitrary but deterministic gap per student and board.

    ``displacement_weight`` > 0 also charges students for every session that has
    left its original slot, so relocations that please the instructor cost them.
    """

    def gap_of(sid, states):
        base = int(sid[1:]) + 1
        gap = 100 * base
        for s in states:
            for m in s.meetings:
                gap += ((m.day * 7 + m.start_min // 15) * base) % 40
                day0, start0 = _ORIGINAL_SLOT[s.course_code]
                gap += displacement_weight * (
                    abs(m.day - day0) * 8 + abs(m.start_min - start0) // 15
                )
        return gap

    return gap_of


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_compaction")
@pytest.mark.parametrize("scale", [0, 6])
def test_with_the_rule_off_the_pass_is_unchanged(monkeypatch, settings, scale):
    """The safety property: ENABLED=false leaves the pass exactly as it was.

    The expected board and counters were captured by running this same fixture on
    the pre-change compaction module (origin/master d584445), with the other
    day-trade settings at their defaults and at deliberately loud values.
    """
    from core.models import SectionPlacement
    from core.services.timetable_assignment_models import RiskTier
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    # Loud values prove they are inert while the rule is off.
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = False
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 10_000
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_PER_STUDENT = 10_000
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_HIGH_RISK_PER_STUDENT = 10_000
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_TOTAL_GAP = 10_000_000
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_VACATE_DAYS = True

    tiers = {"S0": RiskTier.A, "S1": RiskTier.B, "S2": RiskTier.C, "S3": RiskTier.C}
    _install_students(monkeypatch, tiers=tiers, gap_of=_hashed_gap(scale))
    scenario = _board(
        [
            ("C1", "MON", "09:00", "10:15"),
            ("C2", "MON", "14:30", "15:45"),
            ("C3", "TUE", "10:30", "11:45"),
            ("C4", "WED", "09:00", "10:15"),
            ("C5", "WED", "14:30", "15:45"),
        ]
    )

    report = compact_instructor_schedules(scenario.id)

    board = sorted(
        SectionPlacement.objects.filter(board__scenario=scenario).values_list(
            "term_section__course_code", "day", "start_time", "end_time"
        )
    )
    observed = {
        "board": board,
        "moves_evaluated": report["search"]["moves_evaluated"],
        "moves_accepted": report["search"]["moves_accepted"],
        "days": [
            report["instructor_impact"]["working_days_before"],
            report["instructor_impact"]["working_days_after"],
        ],
        "idle": [
            report["instructor_impact"]["total_idle_before"],
            report["instructor_impact"]["total_idle_after"],
        ],
        "gap": [
            report["student_impact"]["total_gap_before"],
            report["student_impact"]["total_gap_after"],
        ],
    }
    assert observed == _RULE_OFF_GOLDEN[scale]


_RULE_OFF_GOLDEN = {
    6: {
        "board": [
            ("C1", "MON", "09:00", "10:15"),
            ("C2", "MON", "14:30", "15:45"),
            ("C3", "MON", "10:30", "11:45"),
            ("C4", "WED", "09:00", "10:15"),
            ("C5", "WED", "14:30", "15:45"),
        ],
        "moves_evaluated": 147,
        "moves_accepted": 1,
        "days": [3, 2],
        "idle": [510, 435],
        "gap": [2400, 2218],
    },
    0: {
        "board": [
            ("C1", "MON", "09:00", "10:15"),
            ("C2", "WED", "10:30", "11:45"),
            ("C3", "MON", "10:30", "11:45"),
            ("C4", "WED", "09:00", "10:15"),
            ("C5", "MON", "13:00", "14:15"),
        ],
        "moves_evaluated": 291,
        "moves_accepted": 3,
        "days": [3, 2],
        "idle": [510, 105],
        "gap": [1440, 1390],
    },
}
