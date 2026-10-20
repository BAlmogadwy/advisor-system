"""Compaction under the day-off policy (R5 and the policy's step, C1-C6).

Only a FIRST day off (an instructor going from 5 working days to 4) may spend
the day trade's student budgets; a second or third day off never costs
students; no instructor's days rise after the search; one instructor's idle may
rise when the total falls (R4); and R3 is measured against the run's start,
not the pass's entry. The pass runs inside a ``day_off_scope`` the way the
optimiser runs it; teachers come from course links (the union charging).
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from core.services.timetable_assignment_models import RiskTier
from core.services.timetable_optimisation_policy import (
    DayOffAnchor,
    DayOffPolicy,
    day_off_scope,
    instructor_schedule_metrics,
)

SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:30", "end": "11:45"},
    {"start": "13:00", "end": "14:15"},
    {"start": "14:30", "end": "15:45"},
]
DAYS = ("SUN", "MON", "TUE", "WED", "THU")


@pytest.fixture
def compaction(settings):
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 6
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 255
    return settings


def _scenario(sessions, teachers, *, slots=SLOTS, locked=()):
    """``sessions`` ``[(code, day, start, end)]``; ``teachers`` maps each code to
    the names that teach it (all of them are charged); ``locked`` codes keep
    their time. Returns the scenario and ``{name: instructor id (str)}``."""
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
    from core.services.timetable_pr4_instructor import normalise_instructor

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="day off compaction",
        gender="M",
        programs=["AI"],
        slot_config=slots,
        lab_slot_config=[],
        blocked_slots=[],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    for n in range(1, 4):
        Room.objects.create(
            room_code=f"R{n}", department="AI", capacity=50, room_type="lecture", section="M"
        )
    people = {}
    for name in sorted({name for names in teachers.values() for name in names}):
        people[name] = Instructor.objects.create(
            full_name=name, normalised_name=normalise_instructor(name)
        )
    for code, names in teachers.items():
        for k, name in enumerate(names):
            CourseInstructor.objects.create(
                program="AI",
                course_code=code,
                section="M",
                instructor=people[name],
                role="primary" if k == 0 else "co",
            )
    for k, (code, day, start, end) in enumerate(sessions):
        ts = TermSection.objects.create(
            scenario=scenario,
            course_key=code,
            section="S1",
            course_code=code,
            course_number=code,
            course_name=code,
            available_capacity=30,
            source_tag="day_off_test",
        )
        room = f"R{k % 3 + 1}"
        TermSectionMeeting.objects.create(
            term_section=ts, day=day, start_time=start, end_time=end, room=room, instructor=""
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            room=room,
            is_locked=code in locked,
        )
    return scenario, {name: str(person.id) for name, person in people.items()}


def _students(monkeypatch, gap_of, ids=("S1",), tier=RiskTier.C):
    """Stand-in student evaluation: each student's gap is ``gap_of(id, states)``
    and the score's gap position is their total, so R5 sees a student cost.
    The instructor schedule is the real one."""
    from core.services import timetable_candidate_eval, timetable_optimizer_v2
    from core.services.timetable_assignment_models import (
        StudentAssignmentState,
        TimetableEvaluationResult,
    )

    profiles = {sid: SimpleNamespace(risk_tier=tier) for sid in ids}
    monkeypatch.setattr(
        timetable_optimizer_v2, "build_student_profiles_for_scenario", lambda _sid: profiles
    )

    def evaluate(
        candidate_id, states, profs, rigidity, section_instructor_ids=None, course_tiers=None
    ):
        per = {
            sid: StudentAssignmentState(sid, total_gap_minutes=gap_of(sid, states)) for sid in profs
        }
        total = sum(state.total_gap_minutes for state in per.values())
        return TimetableEvaluationResult(
            candidate_id,
            (0, 0, 0, 0, total, 0),
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


def _start(scenario, *, score=None, start_days=None, start_idle=None) -> DayOffPolicy:
    """The run's start: this board (or ``start_days``/``start_idle`` per name
    id, as if the search had moved on since), with ``score``."""
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_optimizer_v2 import (
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
    )

    states = build_section_states_for_scenario(scenario.id)
    teachers = build_section_instructor_map_for_scenario(scenario.id)
    anchor = DayOffAnchor.from_metrics(instructor_schedule_metrics(states, teachers), padded=False)
    if start_days or start_idle:
        idle = {**anchor.idle, **(start_idle or {})}
        anchor = dataclasses.replace(
            anchor,
            days={**anchor.days, **(start_days or {})},
            idle=idle,
            total_idle=sum(idle.values()),
        )
    if score is None:  # the board's own score, in the layout the pass scores with
        from core.services.timetable_flags import is_tiered_objective_enabled
        from core.services.timetable_optimizer_v2 import build_course_tier_map_for_scenario

        tiers = (
            build_course_tier_map_for_scenario(scenario.id)
            if is_tiered_objective_enabled()
            else None
        )
        score = evaluate_generated_timetable_candidate(
            "start", states, {}, {}, teachers, tiers
        ).lexicographic_score
    return DayOffPolicy(anchor, tuple(score), "test")


def _compact(scenario, policy):
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    with day_off_scope(policy):
        return compact_instructor_schedules(scenario.id)


def _where(scenario, code) -> tuple[str, str]:
    from core.models import SectionPlacement

    p = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code=code)
    return p.day, p.start_time


def _days_of(scenario, ids) -> dict[str, int]:
    from core.services.timetable_optimizer_v2 import (
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
    )

    metrics = instructor_schedule_metrics(
        build_section_states_for_scenario(scenario.id),
        build_section_instructor_map_for_scenario(scenario.id),
    )["working_days_by_instructor"]
    return {name: metrics.get(i, 0) for name, i in ids.items()}


def _days_used(states) -> set[int]:
    return {m.day for s in states for m in s.meetings}


# ── C1: the student budgets buy a FIRST day off ───────────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize(("cost", "saved"), [(300, True), (301, False)])
def test_c1_a_first_day_off_may_spend_the_trade_budget_exactly(
    compaction, monkeypatch, cost, saved
) -> None:
    _students(
        monkeypatch, lambda _sid, states: 1000 + (cost if len(_days_used(states)) == 4 else 0)
    )
    sessions = [(f"Y{k}", day, "09:00", "10:15") for k, day in enumerate(DAYS)]
    sc, ids = _scenario(sessions, {code: ["Dr Y"] for code, *_ in sessions})
    policy = _start(sc, score=(0, 0, 0, 0, 1000, 0))

    _compact(sc, policy)

    assert _days_of(sc, ids)["Dr Y"] == (4 if saved else 5)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("tier", "cost", "saved"),
    [
        (RiskTier.C, 500, True),
        (RiskTier.C, 501, False),
        (RiskTier.A, 100, True),
        (RiskTier.A, 101, False),
        (RiskTier.B, 101, False),
    ],
)
def test_c1_the_per_student_caps_bound_a_first_day_off_exactly(
    compaction, monkeypatch, tier, cost, saved
) -> None:
    """The day trade's per-student caps (500; 100 here for high-risk and
    graduating students) bound a first day off as they did every saved day."""
    compaction.TIMETABLE_INSTRUCTOR_DAY_TRADE_TOTAL_GAP = 10**6
    compaction.TIMETABLE_INSTRUCTOR_DAY_TRADE_PER_STUDENT = 500
    compaction.TIMETABLE_INSTRUCTOR_DAY_TRADE_HIGH_RISK_PER_STUDENT = 100
    _students(
        monkeypatch,
        lambda _sid, states: 1000 + (cost if len(_days_used(states)) == 4 else 0),
        tier=tier,
    )
    sessions = [(f"Y{k}", day, "09:00", "10:15") for k, day in enumerate(DAYS)]
    sc, ids = _scenario(sessions, {code: ["Dr Y"] for code, *_ in sessions})
    policy = _start(sc, score=(0, 0, 0, 0, 1000, 0))

    _compact(sc, policy)

    assert _days_of(sc, ids)["Dr Y"] == (4 if saved else 5)


# ── C2: a second or third day off never costs students ────────────────────


# Tuesday is fixed, so the one way to gain is to empty Monday or Wednesday.
TUESDAY = ("X2", "X3")
SPARE = [
    ("X1", "MON", "09:00", "10:15"),
    ("X2", "TUE", "09:00", "10:15"),
    ("X3", "TUE", "14:30", "15:45"),
    ("X4", "WED", "10:30", "11:45"),
]


@pytest.mark.django_db
@pytest.mark.parametrize(("cost", "saved"), [(1, False), (0, True)])
def test_c2_a_spare_day_saved_must_not_cost_students(compaction, monkeypatch, cost, saved) -> None:
    """Emptying Wednesday (or Monday) onto Tuesday also cuts Dr X's idle by 75
    minutes; one gap minute for a student still refuses it."""
    _students(
        monkeypatch, lambda _sid, states: 1000 + (cost if len(_days_used(states)) == 2 else 0)
    )
    sc, ids = _scenario(SPARE, {code: ["Dr X"] for code, *_ in SPARE}, locked=TUESDAY)
    policy = _start(sc, score=(0, 0, 0, 0, 1000, 0))

    _compact(sc, policy)

    assert _days_of(sc, ids)["Dr X"] == (2 if saved else 3)


@pytest.mark.django_db
def test_c2_a_spare_day_never_reaches_the_trade_budgets(compaction, monkeypatch) -> None:
    """Students level in total but one of them 200 minutes worse: only the trade
    budget (500 a student) would allow it, and a spare day never gets it."""

    def gap_of(sid, states):
        if len(_days_used(states)) != 2:
            return 1000
        return 1200 if sid == "S1" else 800

    _students(monkeypatch, gap_of, ids=("S1", "S2"))
    sc, ids = _scenario(SPARE, {code: ["Dr X"] for code, *_ in SPARE}, locked=TUESDAY)
    policy = _start(sc, score=(0, 0, 0, 0, 2000, 0))

    _compact(sc, policy)

    assert _days_of(sc, ids)["Dr X"] == 3


# ── C3: a move that gives a first day off may also save a spare one ───────


@pytest.mark.django_db
def test_c3_a_mixed_move_counts_as_a_first_day_off(compaction, monkeypatch) -> None:
    """K (taught by Dr X and Dr Y) leaves Sunday: Dr Y 5 -> 4 days, Dr X 3 -> 2,
    and Dr X's Monday idle falls. Students pay 100 minutes."""
    compaction.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 1
    _students(monkeypatch, lambda _sid, states: 1000 + (0 if 0 in _days_used(states) else 100))
    sessions = [
        ("K", "SUN", "09:00", "10:15"),
        *[(f"Y{k}", day, "09:00", "10:15") for k, day in enumerate(DAYS[1:], start=1)],
        ("X1", "MON", "09:00", "10:15"),
        ("X2", "MON", "13:00", "14:15"),
        ("X3", "TUE", "13:00", "14:15"),
    ]
    teachers = {"K": ["Dr Y", "Dr X"], **{f"Y{k}": ["Dr Y"] for k in range(1, 5)}}
    teachers.update({code: ["Dr X"] for code in ("X1", "X2", "X3")})
    sc, ids = _scenario(sessions, teachers, locked=("X2", "X3"))
    policy = _start(sc, score=(0, 0, 0, 0, 1000, 0))

    _compact(sc, policy)

    assert _where(sc, "K") == ("MON", "10:30")
    assert _days_of(sc, ids) == {"Dr X": 2, "Dr Y": 4}


# ── C4: after the search no instructor's days rise ────────────────────────


@pytest.mark.django_db
def test_c4_a_first_day_off_is_never_paid_with_another_teachers_day(
    compaction, monkeypatch
) -> None:
    """K (Dr X and Dr Y) on Sunday. Moving it to Tuesday would give Dr Y a first
    day off, cut Dr X's idle by 165 and please students — and give Dr X a
    third working day."""
    compaction.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 1

    def gap_of(_sid, states):
        k = next(s for s in states if s.course_code == "K")
        return 1000 - (1 if k.meetings[0].day != 0 else 0)

    _students(monkeypatch, gap_of)
    sessions = [
        ("K", "SUN", "09:00", "10:15"),
        *[(f"Y{k}", day, "09:00", "10:15") for k, day in enumerate(DAYS[1:], start=1)],
        ("X1", "SUN", "13:00", "14:15"),
        ("X2", "MON", "13:00", "14:15"),
    ]
    teachers = {"K": ["Dr Y", "Dr X"], **{f"Y{k}": ["Dr Y"] for k in range(1, 5)}}
    teachers.update({"X1": ["Dr X"], "X2": ["Dr X"]})
    sc, ids = _scenario(sessions, teachers, locked=("X1", "X2"))
    policy = _start(sc, score=(0, 0, 0, 0, 1000, 0))

    _compact(sc, policy)

    days = _days_of(sc, ids)
    assert days["Dr X"] == 2 and days["Dr Y"] == 4


# ── C5: one teacher's idle may rise when the total falls (R4) ─────────────


R4_SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:50", "end": "12:05"},
    {"start": "13:15", "end": "14:30"},
    {"start": "14:30", "end": "15:45"},
]


@pytest.mark.django_db
def test_c5_one_teachers_idle_rises_twenty_while_anothers_falls_ninety(compaction) -> None:
    """L (Dr J and Dr K) to Monday 10:50: Dr J +20 idle minutes, Dr K -90.
    Nobody's days change and the total falls; every other slot either clashes,
    adds a day or leaves the total no lower."""
    compaction.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = False
    sessions = [
        ("L", "SUN", "10:30", "11:45"),
        ("J1", "SUN", "09:00", "10:15"),
        ("J2", "MON", "09:00", "10:15"),
        ("K1", "SUN", "13:15", "14:30"),
        ("K2", "MON", "12:05", "13:20"),
    ]
    teachers = {"L": ["Dr J", "Dr K"], "J1": ["Dr J"], "J2": ["Dr J"]}
    teachers.update({"K1": ["Dr K"], "K2": ["Dr K"]})
    sc, ids = _scenario(sessions, teachers, slots=R4_SLOTS, locked=("J1", "J2", "K1", "K2"))
    policy = _start(sc)

    report = _compact(sc, policy)

    assert _where(sc, "L") == ("MON", "10:50")
    impact = report["instructor_impact"]
    before, after = (
        impact["idle_minutes_by_instructor_before"],
        impact["idle_minutes_by_instructor_after"],
    )
    assert after[ids["Dr J"]] - before[ids["Dr J"]] == 20
    assert after[ids["Dr K"]] - before[ids["Dr K"]] == -90


# ── C6: R3 is measured against the run start, not the pass entry ──────────


C6_SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:45", "end": "12:00"},
    {"start": "13:15", "end": "14:30"},
    {"start": "14:45", "end": "16:00"},
]


@pytest.mark.django_db
def test_c6_a_spare_day_given_up_earlier_keeps_its_sixty_minutes(compaction) -> None:
    """Dr P started the run on 3 days; the search bought a 4th for students at
    40 idle minutes. Moving L to Monday would cost Dr P 30 more (70 > 60) to
    spare Dr Q 105: refused, though measured from the pass entry it is an
    ordinary R4 trade."""
    compaction.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = False
    sessions = [
        ("L", "SUN", "10:15", "11:30"),
        ("P1", "SUN", "09:00", "10:15"),
        ("P2", "MON", "09:00", "10:15"),
        ("P3", "TUE", "09:00", "10:15"),
        ("P4", "TUE", "10:55", "12:10"),
        ("P5", "WED", "09:00", "10:15"),
        ("Q1", "SUN", "13:15", "14:30"),
        ("Q2", "MON", "12:00", "13:15"),
    ]
    teachers = {"L": ["Dr P", "Dr Q"], **{f"P{k}": ["Dr P"] for k in range(1, 6)}}
    teachers.update({"Q1": ["Dr Q"], "Q2": ["Dr Q"]})
    locked = tuple(code for code, *_ in sessions if code != "L")
    sc, ids = _scenario(sessions, teachers, slots=C6_SLOTS, locked=locked)
    board_score = _start(sc).score
    gained = board_score[:4] + (board_score[4] + 1,) + board_score[5:]  # the start was worse
    policy = _start(sc, score=gained, start_days={ids["Dr P"]: 3}, start_idle={ids["Dr P"]: 0})

    _compact(sc, policy)

    assert _where(sc, "L") == ("SUN", "10:15")
