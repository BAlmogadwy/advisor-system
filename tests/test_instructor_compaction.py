"""Tests for the post-build instructor-day compaction pass.

The pass shrinks each instructor's within-day idle gaps by relocating their
sessions in time, strictly guarded so students/feasibility never regress and
flag-gated (default off). Synthetic boards exercise relocation, protected
score components, student cost limits, and a replay with real student demand.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import override_settings

from core.services.timetable_assignment_models import (
    RiskTier,
    StudentAssignmentState,
    StudentProfile,
)

_SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:30", "end": "11:45"},
    {"start": "13:00", "end": "14:15"},
    {"start": "14:30", "end": "15:45"},
]


def _gappy_board(*, lab=False):
    """One instructor teaching two MON courses with a large midday hole."""
    from core.models import (
        CourseInstructor,
        DeliveryBoard,
        Instructor,
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
        name="AI M T1 compact",
        gender="M",
        programs=["AI"],
        slot_config=[] if lab else _SLOTS,
        lab_slot_config=[
            {"start": "09:00", "end": "10:40"},
            {"start": "10:45", "end": "12:25"},
            {"start": "13:00", "end": "14:40"},
        ]
        if lab
        else [],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    instr = Instructor.objects.create(
        full_name="Dr Gap", normalised_name=normalise_instructor("Dr Gap")
    )
    placements = [("C1", "09:00", "10:15"), ("C2", "13:00", "14:15")]  # MON, 2h45 hole
    if lab:
        placements = [("C1", "09:00", "10:40"), ("C2", "13:00", "14:40")]
    for code, start, end in placements:
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
            source_tag="compact_test",
        )
        TermSectionMeeting.objects.create(
            term_section=ts, day="MON", start_time=start, end_time=end, room="", instructor=""
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day="MON",
            start_time=start,
            end_time=end,
            room="R1",
            is_locked=False,
        )
        apply_primary_instructor(ts, scenario, board, ts.course_code)
    return scenario


def _instructor_idle(scenario):
    """Total within-day idle minutes for the (single) instructor."""
    from collections import defaultdict

    from core.models import SectionPlacement, TermSectionMeeting

    instr = {
        ts: nm
        for ts, nm in TermSectionMeeting.objects.filter(term_section__scenario=scenario)
        .exclude(instructor="")
        .values_list("term_section_id", "instructor")
    }
    byday = defaultdict(list)
    for p in SectionPlacement.objects.filter(board__scenario=scenario).exclude(day=""):
        nm = instr.get(p.term_section_id)
        if nm:
            h, m = p.start_time.split(":")
            he, me = p.end_time.split(":")
            byday[(nm, p.day)].append((int(h) * 60 + int(m), int(he) * 60 + int(me)))
    total = 0
    for sess in byday.values():
        sess = sorted(sess)
        total += sum(
            g for g in (sess[i + 1][0] - sess[i][1] for i in range(len(sess) - 1)) if g > 0
        )
    return total


def _placement_snapshot(scenario):
    from core.models import SectionPlacement, TermSectionMeeting

    return (
        list(
            SectionPlacement.objects.filter(board__scenario=scenario)
            .order_by("pk")
            .values_list("term_section_id", "day", "start_time", "end_time", "room")
        ),
        list(
            TermSectionMeeting.objects.filter(term_section__scenario=scenario)
            .order_by("pk")
            .values_list("term_section_id", "day", "start_time", "end_time", "instructor")
        ),
    )


@pytest.fixture
def _compaction_enabled(settings):
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 4
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_GAP_BUDGET = 0.03
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_PER_STUDENT_CAP = 75
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TRADE_RATIO = 2.0


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_compaction_reduces_idle() -> None:
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    assert _instructor_idle(scenario) == 165  # 2h45 hole before

    report = compact_instructor_schedules(scenario.id)

    assert report["enabled"] is True
    assert (
        report["instructor_impact"]["total_idle_after"]
        < report["instructor_impact"]["total_idle_before"]
    )
    assert report["search"]["moves_accepted"] >= 1
    assert _instructor_idle(scenario) < 165  # hole shrunk on the real board
    # No student / feasibility regression (vacuous here, but the gates must hold).
    assert report["protected"]["feasibility_after"] == report["protected"]["feasibility_before"]


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_compaction_is_idempotent() -> None:
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    compact_instructor_schedules(scenario.id)
    idle_after_first = _instructor_idle(scenario)

    second = compact_instructor_schedules(scenario.id)
    assert second["search"]["moves_accepted"] == 0  # already compact → no more moves
    assert _instructor_idle(scenario) == idle_after_first


@pytest.mark.django_db(transaction=True)
@override_settings(TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=False)
def test_compaction_noop_when_flag_off(django_assert_num_queries) -> None:
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    before = _placement_snapshot(scenario)
    with django_assert_num_queries(0):
        report = compact_instructor_schedules(scenario.id)
    assert report == {"enabled": False}
    assert _placement_snapshot(scenario) == before
    assert _instructor_idle(scenario) == 165  # untouched when off


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction_enabled")
@pytest.mark.parametrize(
    ("score_length", "worsened_index"),
    [
        pytest.param(length, index, id=f"layout-{length}-metric-{index}")
        for length in (6, 7, 9)
        for index in (*range(4), 6 if length == 9 else 5)
    ]
    + [pytest.param(9, 5, id="tiered-soft-unresolved")],
)
def test_compaction_rejects_each_protected_score_regression(
    monkeypatch, settings, score_length, worsened_index
) -> None:
    """An earlier improvement cannot pay for any protected metric worsening."""
    from core.services import timetable_candidate_eval
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = score_length == 9
    settings.TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED = score_length == 7
    scenario = _gappy_board()
    before = _placement_snapshot(scenario)
    baseline = [4] * score_length
    candidate = baseline.copy()
    if worsened_index != 0:
        candidate[0] -= 1  # lexicographically better, but unsafe
    candidate[worsened_index] += 1
    calls = 0

    def evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            lexicographic_score=tuple(baseline if calls == 1 else candidate),
            assignment_states={},
        )

    monkeypatch.setattr(
        timetable_candidate_eval, "evaluate_generated_timetable_candidate", evaluate
    )
    report = compact_instructor_schedules(scenario.id)

    assert report["search"]["moves_evaluated"] > 0
    assert report["search"]["moves_accepted"] == 0
    assert report["protected"]["feasibility_before"] == baseline[:4]
    assert report["protected"]["feasibility_after"] == baseline[:4]
    assert _placement_snapshot(scenario) == before


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction_enabled")
@pytest.mark.parametrize("lab", [False, True], ids=["lecture", "lab"])
def test_compaction_never_offers_online_only_windows(monkeypatch, lab) -> None:
    from core.services import timetable_candidate_eval
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board(lab=lab)
    field = "lab_slot_config" if lab else "slot_config"
    slots = list(getattr(scenario, field))
    slots.append({"start": "17:40", "end": "19:20" if lab else "18:55", "online_only": True})
    setattr(scenario, field, slots)
    scenario.save(update_fields=[field])
    evaluated_starts = set()
    evaluate = timetable_candidate_eval.evaluate_generated_timetable_candidate

    def inspect_candidate(candidate_id, sections, *args, **kwargs):
        evaluated_starts.update(m.start_min for s in sections for m in s.meetings)
        return evaluate(candidate_id, sections, *args, **kwargs)

    monkeypatch.setattr(
        timetable_candidate_eval, "evaluate_generated_timetable_candidate", inspect_candidate
    )
    report = compact_instructor_schedules(scenario.id)

    assert report["search"]["moves_evaluated"] > 0
    assert report["search"]["moves_accepted"] > 0
    assert 17 * 60 + 40 not in evaluated_starts
    assert all(row[2] != "17:40" for row in _placement_snapshot(scenario)[0])


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction_enabled")
@pytest.mark.parametrize(
    ("tiers", "before_gaps", "after_gaps"),
    [
        pytest.param([RiskTier.C], [100], [104], id="total-budget"),
        pytest.param([RiskTier.A, RiskTier.C], [100, 100], [101, 99], id="high-risk-gap"),
        pytest.param([RiskTier.B, RiskTier.C], [100, 100], [101, 99], id="graduating-gap"),
        pytest.param([RiskTier.C, RiskTier.C], [100, 100], [176, 24], id="per-student-cap"),
        pytest.param([RiskTier.C, RiskTier.C], [10000, 10000], [10060, 10060], id="trade-ratio"),
    ],
)
def test_compaction_preserves_student_gap_guards(
    monkeypatch, tiers, before_gaps, after_gaps
) -> None:
    from core.services import timetable_candidate_eval, timetable_optimizer_v2
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    before = _placement_snapshot(scenario)
    profiles = {
        str(i): StudentProfile(str(i), "AI", ["C1", "C2"], tier, 1.0)
        for i, tier in enumerate(tiers)
    }
    monkeypatch.setattr(
        timetable_optimizer_v2, "build_student_profiles_for_scenario", lambda _: profiles
    )
    calls = 0

    def evaluate(*args, **kwargs):
        nonlocal calls
        gaps = before_gaps if calls == 0 else after_gaps
        calls += 1
        return SimpleNamespace(
            lexicographic_score=(0, 0, 0, 0, sum(gaps), 0),
            assignment_states={
                str(i): StudentAssignmentState(str(i), total_gap_minutes=gap)
                for i, gap in enumerate(gaps)
            },
        )

    monkeypatch.setattr(
        timetable_candidate_eval, "evaluate_generated_timetable_candidate", evaluate
    )
    report = compact_instructor_schedules(scenario.id)

    assert report["search"]["moves_evaluated"] > 0
    assert report["search"]["moves_accepted"] == 0
    assert report["student_impact"]["total_gap_before"] == sum(before_gaps)
    assert report["student_impact"]["total_gap_after"] == sum(before_gaps)
    assert _placement_snapshot(scenario) == before


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction_enabled")
@pytest.mark.parametrize("score_length", [6, 7, 9], ids=["legacy", "legacy-idle", "tiered"])
def test_compaction_replay_with_student_demand_preserves_assignments(
    settings, score_length
) -> None:
    """Real demand adapters, assignment evaluator, relocation and persistence."""
    from core.models import (
        Course,
        CourseInstructor,
        ScenarioStudentCourseRequest,
        Student,
        StudentCourse,
    )
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_instructor_compaction import compact_instructor_schedules
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )
    from core.services.timetable_student_assignment import reserve_used_of

    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = score_length == 9
    settings.TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED = score_length == 7
    scenario = _gappy_board()
    for sid, earned in [(1, 20), (2, 110), (3, 20)]:
        Student.objects.create(
            student_id=sid, program="AI", section="M", total_earned_credits=earned
        )
        for code in ("C1", "C2"):
            ScenarioStudentCourseRequest.objects.create(
                scenario=scenario, student_id=sid, course_key=code, course_code=code
            )
    StudentCourse.objects.create(
        student_id=1, course=Course.objects.create(course_code="C1"), status="failed", grade="F"
    )
    profiles = build_student_profiles_for_scenario(scenario.id)
    assert {p.risk_tier for p in profiles.values()} == {RiskTier.A, RiskTier.B, RiskTier.C}
    instructor_ids = build_section_instructor_map_for_scenario(scenario.id)
    assignments_before = list(CourseInstructor.objects.order_by("pk").values())
    meeting_instructors_before = [(r[0], r[4]) for r in _placement_snapshot(scenario)[1]]
    course_tiers = build_course_tier_map_for_scenario(scenario.id) if score_length == 9 else None

    def evaluate():
        return evaluate_generated_timetable_candidate(
            "student-replay",
            build_section_states_for_scenario(scenario.id),
            profiles,
            build_course_rigidity_for_scenario(scenario.id),
            section_instructor_ids=instructor_ids,
            course_tiers=course_tiers,
        )

    baseline = evaluate()
    assert len(baseline.lexicographic_score) == score_length
    assert sum(s.total_gap_minutes for s in baseline.assignment_states.values()) == 495

    report = compact_instructor_schedules(scenario.id)
    final = evaluate()

    assert report["search"]["moves_accepted"] > 0
    assert report["instructor_impact"]["total_idle_after"] < 165
    assert len(final.assignment_states) == 3
    assert final.unresolved_student_ids == []
    for sid, state in final.assignment_states.items():
        assert set(state.assigned_sections) == {"C1", "C2"}
        assert state.total_gap_minutes < baseline.assignment_states[sid].total_gap_minutes
    assert all(
        a <= b
        for a, b in zip(
            final.lexicographic_score[:4], baseline.lexicographic_score[:4], strict=True
        )
    )
    assert reserve_used_of(final.lexicographic_score) == reserve_used_of(
        baseline.lexicographic_score
    )
    assert final.instructor_overload_count == 0
    assert report["student_impact"]["students_worsened"] == 0
    assert report["student_impact"]["students_improved"] == 3
    assert build_section_instructor_map_for_scenario(scenario.id) == instructor_ids
    assert list(CourseInstructor.objects.order_by("pk").values()) == assignments_before
    assert [(r[0], r[4]) for r in _placement_snapshot(scenario)[1]] == meeting_instructors_before
