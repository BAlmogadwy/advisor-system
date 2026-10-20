"""Secondary recovery under the day-off policy: the persist re-check ratchet.

Recovery freezes every instructor's days and idle (neither may rise), yet it
can still turn a sanctioned week into a breach: saving back a day an instructor
gave up for students removes the R3 minutes that covered their idle, and the
same idle then counts as an R4 rise. The persisted board must not grow any
component of the policy's breach vector.
"""

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services import timetable_secondary_recovery_runner as runner
from core.services.timetable_assignment_models import (
    CanonicalPattern,
    RiskTier,
    SectionMeeting,
    StudentProfile,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_optimisation_policy import DayOffAnchor, DayOffPolicy, day_off_scope
from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

pytestmark = pytest.mark.django_db

TEACHERS = {
    "CORE_M1": frozenset({7}),
    "SOFT_M1": frozenset({8}),
    "OTHER_M1": frozenset({8}),
    "OTHER2_M1": frozenset({8}),
}
TIERS = {"CORE": "T1", "SOFT": "T3", "OTHER": "T1", "OTHER2": "T1"}
PROFILES = {"s": StudentProfile("s", "CS", ["CORE", "SOFT"], RiskTier.C, 0)}


@pytest.fixture
def board(settings, monkeypatch):
    """CORE (teacher 7) and SOFT (teacher 8) clash on Sunday 09:00 for the one
    student. Teacher 8 also teaches OTHER and OTHER2 on Monday, 15 idle
    minutes apart. SOFT's only other pattern is Monday 11:45, straight after
    OTHER2: recovery resolves the student, saves teacher 8 Sunday and leaves
    their idle at 15."""
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = True
    settings.TIMETABLE_SECONDARY_RECOVERY_SECONDS = 2
    settings.TIMETABLE_SECONDARY_RECOVERY_ITERATIONS = 2
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="recovery day off", gender="M", programs=["CS"]
    )
    delivery = DeliveryBoard.objects.create(
        scenario=scenario, program="CS", nominal_term=1, label="CS"
    )
    for code in ("R1", "R2"):
        Room.objects.create(
            room_code=code, capacity=40, room_type="lecture", section="M", department="CS"
        )
    for course, day, start, end, locked, room in [
        ("CORE", "SUN", "09:00", "10:15", True, "R1"),
        ("SOFT", "SUN", "09:00", "10:15", False, ""),
        ("OTHER", "MON", "09:00", "10:15", True, "R2"),
        ("OTHER2", "MON", "10:30", "11:45", True, "R2"),
    ]:
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=course,
            course_code=course,
            department="CS",
            credit_hours=3,
            total_demand=5,
            planned_sections=1,
            max_per_section=30,
            programme_term=1,
        )
        section = TermSection.objects.create(
            scenario=scenario,
            course_key=course,
            course_code=course,
            section="M1",
            available_capacity=30,
        )
        SectionPlacement.objects.create(
            board=delivery,
            term_section=section,
            day=day,
            start_time=start,
            end_time=end,
            room=room,
            is_locked=locked,
        )
        TermSectionMeeting.objects.create(
            term_section=section, day=day, start_time=start, end_time=end, room=room
        )
    states = build_section_states_for_scenario(scenario.id)
    soft = next(s for s in states if s.course_code == "SOFT")
    patterns = [
        CanonicalPattern(
            soft.pattern_id,
            "",
            soft.meetings,
            soft.pattern_family,
            "75",
            False,
            1,
            frozenset({0}),
            "",
        ),
        CanonicalPattern(
            "MON-1145",
            "",
            [SectionMeeting(1, 705, 780)],
            soft.pattern_family,
            "75",
            False,
            1,
            frozenset({1}),
            "",
        ),
    ]
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2._build_pattern_catalog_for_scenario",
        lambda _sid: {soft.pattern_family: patterns},
    )
    return scenario


def _policy(scenario, *, days: int, idle: int) -> DayOffPolicy:
    """Teacher 8 started the run on ``days`` with ``idle``; the student score
    in hand is better than the start's."""
    score = evaluate_generated_timetable_candidate(
        "start", build_section_states_for_scenario(scenario.id), PROFILES, {}, TEACHERS, TIERS
    ).lexicographic_score
    anchor = DayOffAnchor(
        days={"7": 1, "8": days},
        idle={"7": 0, "8": idle},
        total_idle=idle,
        padded=True,
        saved_idle=0,
    )
    worse_start = score[:4] + (score[4] + 1000,) + score[5:]
    return DayOffPolicy(anchor, tuple(worse_start), "saved_board")


def _recover(scenario):
    return runner.run_secondary_recovery_for_scenario(
        scenario.id, {}, PROFILES, {}, TEACHERS, TIERS
    )


def _soft_day(scenario) -> str:
    return SectionPlacement.objects.get(term_section__course_code="SOFT").day


def test_saving_back_a_spare_day_cannot_turn_its_idle_into_an_r4_breach(board):
    """Teacher 8 gave up Monday for students (1 -> 2 days) at 15 idle minutes,
    inside R3's 60. Recovery would save Sunday back: 1 day again, the same 15
    minutes now sanctioned by nothing while the total has not fallen."""
    with day_off_scope(_policy(board, days=1, idle=0)):
        report = _recover(board)

    assert report["stop_reason"] == "persistence_guard_rejected"
    assert [r["metric"] for r in report["regressions"]] == ["instructor_day_off_policy"]
    assert _soft_day(board) == "SUN"


@pytest.mark.parametrize("scoped", [False, True])
def test_the_same_recovery_is_kept_when_it_breaches_nothing(board, scoped):
    """Teacher 8 started on 2 days with this idle: Sunday saved is a plain gain."""
    if scoped:
        with day_off_scope(_policy(board, days=2, idle=15)):
            report = _recover(board)
    else:
        report = _recover(board)

    assert report["applied"], report
    assert _soft_day(board) == "MON"
