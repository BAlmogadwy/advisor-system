"""TIMETABLE_GAP_REGULAR_STUDENTS_ONLY - optimise student gap for regular students only.

A "visitor" is a cross-term student (``ScenarioStudentCourseRequest.is_cross_term``,
the flag that drives the "visitor" BoardStudentLinks). With the option OFF (the
default) nothing changes. With it ON, visitors leave the objective's gap term and
the instructor-compaction / day-trade AGGREGATE gap budget; seating, clashes,
T1/T2, high-risk, reserve and every per-student cap still cover everyone.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import override_settings

from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
    StudentProfile,
    UnresolvedReason,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_flags import is_gap_regular_students_only_enabled
from core.services.timetable_student_assignment import (
    TI_CLASH,
    TI_HIGHRISK,
    TI_SOFT,
    TI_STUDENT_COST,
    TI_T1,
    TI_T2_OVER,
    decode_score,
    evaluate_assignability_lexicographic,
    student_gap_split,
)
from core.services.timetable_v2_runner import (
    attach_student_gap_fields,
    restore_baseline_student_gap,
)

ON = {"TIMETABLE_GAP_REGULAR_STUDENTS_ONLY": True}
OFF = {"TIMETABLE_GAP_REGULAR_STUDENTS_ONLY": False}
TIERED = {"TIMETABLE_TIERED_OBJECTIVE_ENABLED": True, "TIMETABLE_TIERED_SOFT_GAP_BUDGET": 120}
LEGACY = {"TIMETABLE_TIERED_OBJECTIVE_ENABLED": False}


def _profile(sid, *, visitor=False, tier=RiskTier.C):
    return StudentProfile(
        student_id=sid,
        department="X",
        recommended_courses=[],
        risk_tier=tier,
        intra_tier_score=1.0,
        is_visitor=visitor,
    )


def _state(sid, gap, unresolved=()):
    st = StudentAssignmentState(student_id=sid)
    st.total_gap_minutes = gap
    st.unresolved_courses = {c: UnresolvedReason(course_code=c, reason="full") for c in unresolved}
    return st


def _score(states, profiles, *, layout, flag):
    tiers = {"T1C": "T1", "T3C": "T3"} if layout is TIERED else None
    with override_settings(**layout, **flag):
        return evaluate_assignability_lexicographic(states, profiles, {}, None, tiers)


def _board():
    """R1, R2 regular; V1 visitor. One unresolved T1 (R2) and one soft T3 (V1)."""
    profiles = {
        "R1": _profile("R1"),
        "R2": _profile("R2", tier=RiskTier.A),
        "V1": _profile("V1", visitor=True),
    }
    states = {
        "R1": _state("R1", 100),
        "R2": _state("R2", 40, ["T1C"]),
        "V1": _state("V1", 700, ["T3C"]),
    }
    return states, profiles


def test_option_is_off_by_default():
    import os

    from django.conf import settings

    if "TIMETABLE_GAP_REGULAR_STUDENTS_ONLY" not in os.environ:
        assert settings.TIMETABLE_GAP_REGULAR_STUDENTS_ONLY is False
    with override_settings(**OFF):
        assert is_gap_regular_students_only_enabled() is False
    with override_settings(**ON):
        assert is_gap_regular_students_only_enabled() is True


# -- flag OFF: byte-identical -------------------------------------------------


@pytest.mark.parametrize(
    ("layout", "golden"),
    [
        # tiered: (high-risk, clashes, T1, T2-over, gap + 120*soft, soft, reserve, spread, idle)
        (TIERED, (1, 0, 1, 0, 100 + 40 + 700 + 120 * 1, 1, 0, 0, 0)),
        # legacy: (tier_a, unres students, unassigned courses, clashes, gap, reserve)
        (LEGACY, (1, 2, 2, 0, 100 + 40 + 700, 0)),
    ],
    ids=["tiered", "legacy"],
)
def test_flag_off_golden_score_counts_every_student(layout, golden):
    states, profiles = _board()
    assert _score(states, profiles, layout=layout, flag=OFF) == golden


@pytest.mark.parametrize("layout", [TIERED, LEGACY], ids=["tiered", "legacy"])
def test_flag_off_ignores_the_visitor_mark_entirely(layout):
    states, profiles = _board()
    unmarked = {sid: _profile(sid, tier=p.risk_tier) for sid, p in profiles.items()}
    assert _score(states, profiles, layout=layout, flag=OFF) == _score(
        states, unmarked, layout=layout, flag=OFF
    )


# -- flag ON: only the gap term moves -----------------------------------------


@pytest.mark.parametrize("layout", [TIERED, LEGACY], ids=["tiered", "legacy"])
def test_on_counts_regular_students_only(layout):
    states, profiles = _board()
    on = _score(states, profiles, layout=layout, flag=ON)
    gap_idx = TI_STUDENT_COST if layout is TIERED else 4
    soft_part = 120 * 1 if layout is TIERED else 0
    assert on[gap_idx] == 100 + 40 + soft_part  # visitor V1's 700 is out


@pytest.mark.parametrize("layout", [TIERED, LEGACY], ids=["tiered", "legacy"])
def test_visitor_gap_change_does_not_move_the_score_but_a_regular_does(layout):
    states, profiles = _board()
    base = _score(states, profiles, layout=layout, flag=ON)

    states["V1"].total_gap_minutes = 5000  # visitor changes: no effect
    assert _score(states, profiles, layout=layout, flag=ON) == base

    states["R1"].total_gap_minutes += 25  # regular changes: moves by exactly 25
    moved = _score(states, profiles, layout=layout, flag=ON)
    gap_idx = TI_STUDENT_COST if layout is TIERED else 4
    assert moved[gap_idx] - base[gap_idx] == 25
    assert [v for i, v in enumerate(moved) if i != gap_idx] == [
        v for i, v in enumerate(base) if i != gap_idx
    ]


@pytest.mark.parametrize("layout", [TIERED, LEGACY], ids=["tiered", "legacy"])
def test_on_changes_nothing_but_the_gap_position(layout):
    states, profiles = _board()
    off = _score(states, profiles, layout=layout, flag=OFF)
    on = _score(states, profiles, layout=layout, flag=ON)
    gap_idx = TI_STUDENT_COST if layout is TIERED else 4
    assert on[gap_idx] == off[gap_idx] - 700
    assert [v for i, v in enumerate(on) if i != gap_idx] == [
        v for i, v in enumerate(off) if i != gap_idx
    ]


def test_visitor_still_counts_in_every_seating_position():
    # Positions 0-3 and the soft count see the visitor exactly as before: an
    # unresolved T1 for a visitor still raises T1, and a visitor clash still counts.
    profiles = {"V1": _profile("V1", visitor=True, tier=RiskTier.A)}
    states = {"V1": _state("V1", 900, ["T1C"])}
    off = _score(states, profiles, layout=TIERED, flag=OFF)
    on = _score(states, profiles, layout=TIERED, flag=ON)
    assert on[TI_T1] == off[TI_T1] == 1
    assert on[TI_HIGHRISK] == off[TI_HIGHRISK] == 1
    assert on[TI_CLASH] == off[TI_CLASH]
    assert on[TI_T2_OVER] == off[TI_T2_OVER]
    assert on[TI_SOFT] == off[TI_SOFT]
    assert on[TI_STUDENT_COST] == 0 and off[TI_STUDENT_COST] == 900


def test_decode_score_reads_the_regular_gap_when_on():
    states, profiles = _board()
    on = _score(states, profiles, layout=TIERED, flag=ON)
    with override_settings(**TIERED):
        assert decode_score(on)["gap_minutes"] == 140


def test_student_gap_split_reports_both():
    states, profiles = _board()
    assert student_gap_split(states, profiles) == {
        "gap_minutes_regular": 140,
        "gap_minutes_visitors": 700,
        "gap_minutes_all": 840,
        "visitor_students": 1,
        "regular_students": 2,
    }


# -- real seating on a small board --------------------------------------------


def _section(sid, course, meetings, cap=10):
    return SectionState(
        section_id=sid,
        course_code=course,
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for d, s, e in meetings],
        max_capacity=cap,
        reserve_capacity=0,
    )


def _seated_board():
    sections = [
        _section("A1", "A", [(0, 480, 555)]),  # Sun 08:00-09:15
        _section("B1", "B", [(0, 720, 795)]),  # Sun 12:00-13:15  -> real gap for A+B
        _section("C1", "C", [(1, 480, 555)], cap=1),  # one seat, three students want it
    ]
    profiles = {
        sid: StudentProfile(
            student_id=sid,
            department="X",
            recommended_courses=["A", "B", "C"],
            risk_tier=RiskTier.C,
            intra_tier_score=score,
            is_visitor=sid.startswith("V"),
        )
        for sid, score in (("R1", 1.0), ("R2", 2.0), ("V1", 3.0))
    }
    return sections, profiles


def _evaluate(flag):
    sections, profiles = _seated_board()
    with override_settings(**TIERED, **flag):
        return evaluate_generated_timetable_candidate(
            "c", sections, profiles, {"A": 0.0, "B": 0.0, "C": 1.0}, None, {"A": "T1", "B": "T1"}
        )


def test_seating_is_identical_on_and_off_only_the_gap_term_differs():
    off, on = _evaluate(OFF), _evaluate(ON)
    assert {
        s: (st.assigned_sections, st.unresolved_courses) for s, st in on.assignment_states.items()
    } == {
        s: (st.assigned_sections, st.unresolved_courses) for s, st in off.assignment_states.items()
    }
    gap_all = sum(st.total_gap_minutes for st in off.assignment_states.values())
    visitor_gap = on.assignment_states["V1"].total_gap_minutes
    assert visitor_gap > 0
    assert off.lexicographic_score[TI_STUDENT_COST] - on.lexicographic_score[TI_STUDENT_COST] == (
        visitor_gap
    )
    assert gap_all == sum(st.total_gap_minutes for st in on.assignment_states.values())
    for i in range(len(off.lexicographic_score)):
        if i != TI_STUDENT_COST:
            assert on.lexicographic_score[i] == off.lexicographic_score[i]


def test_reports_carry_both_gaps_only_when_on():
    on, off = _evaluate(ON), _evaluate(OFF)
    assert "student_gap" not in off.quality_score
    sg = on.quality_score["student_gap"]
    assert sg["regular_only"] is True
    assert sg["gap_minutes_all"] == sum(
        st.total_gap_minutes for st in on.assignment_states.values()
    )
    assert sg["gap_minutes_regular"] + sg["gap_minutes_visitors"] == sg["gap_minutes_all"]
    assert sg["gap_minutes_visitors"] == on.assignment_states["V1"].total_gap_minutes > 0
    assert (
        on.lexicographic_score[TI_STUDENT_COST]
        == sg["gap_minutes_regular"] + 120 * on.lexicographic_score[TI_SOFT]
    )


def test_result_fields_hoisted_only_when_present_and_restored_on_rollback():
    result = {"quality_score": {"penalty": 3}}
    attach_student_gap_fields(result)
    assert not any(k.startswith("gap_") for k in result)  # OFF payload untouched

    split = {
        "regular_only": True,
        "gap_minutes_regular": 10,
        "gap_minutes_visitors": 5,
        "gap_minutes_all": 15,
    }
    result = {"quality_score": {"student_gap": dict(split)}, "baseline_student_gap": dict(split)}
    attach_student_gap_fields(result)
    assert (
        result["gap_minutes_regular"],
        result["gap_minutes_visitors"],
        result["gap_minutes_all"],
        result["gap_regular_students_only"],
    ) == (10, 5, 15, True)

    result["quality_score"]["student_gap"] = {**split, "gap_minutes_regular": 99}
    restore_baseline_student_gap(result)
    assert result["quality_score"]["student_gap"]["gap_minutes_regular"] == 10


# -- profiles come from the persisted cross-term flag -------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(("flag", "expected"), [(ON, True), (OFF, False)], ids=["on", "off"])
def test_profiles_mark_cross_term_students_only_when_on(flag, expected):
    from core.models import ScenarioStudentCourseRequest, TimetableScenario
    from core.services.timetable_optimizer_v2 import build_student_profiles_for_scenario

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="gap regular", gender="M", programs=["AI"]
    )
    rows = [
        (101, "C1", False),
        (101, "C2", False),
        (202, "C1", True),  # cross-term: courses on more than one term board
        (202, "C3", True),
    ]
    for sid, code, cross in rows:
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario,
            student_id=sid,
            course_key=code,
            course_code=code,
            is_cross_term=cross,
        )
    with override_settings(**flag):
        profiles = build_student_profiles_for_scenario(scenario.id)
    assert profiles["101"].is_visitor is False
    assert profiles["202"].is_visitor is expected


# -- compaction / day-trade aggregate vs per-student protections ---------------


@pytest.fixture
def _compaction(settings):
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 6
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0


def _saved_days(settings, monkeypatch, *, students, extra, regular_only, **caps):
    """Run the real day-trade pass; ``students`` is ``{id: (RiskTier, is_visitor)}``."""
    from core.services import timetable_optimizer_v2
    from core.services.timetable_instructor_compaction import compact_instructor_schedules
    from tests.test_instructor_day_trade import (
        _board,
        _days_used,
        _gap_when_a_day_is_saved,
        _install_students,
    )

    settings.TIMETABLE_GAP_REGULAR_STUDENTS_ONLY = regular_only
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE = 150
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_PER_STUDENT = caps.get("per_student", 10**6)
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_HIGH_RISK_PER_STUDENT = caps.get("high_risk", 10**6)
    settings.TIMETABLE_INSTRUCTOR_DAY_TRADE_TOTAL_GAP = caps.get("total", 300)
    _install_students(
        monkeypatch,
        tiers={sid: tier for sid, (tier, _v) in students.items()},
        gap_of=_gap_when_a_day_is_saved(extra),
    )
    profiles = {
        sid: SimpleNamespace(risk_tier=tier, is_visitor=visitor)
        for sid, (tier, visitor) in students.items()
    }
    monkeypatch.setattr(
        timetable_optimizer_v2, "build_student_profiles_for_scenario", lambda _sid: profiles
    )
    scenario = _board([("C1", "MON", "09:00", "10:15"), ("C2", "TUE", "09:00", "10:15")])
    compact_instructor_schedules(scenario.id)
    return _days_used(scenario)


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
@pytest.mark.parametrize(("regular_only", "days"), [(False, 2), (True, 1)])
def test_aggregate_trade_budget_ignores_visitors_only_when_on(
    settings, monkeypatch, regular_only, days
):
    # The visitor pays 5000 extra, the regular student 100; the total cap is 300.
    students = {"V1": (RiskTier.C, True), "R1": (RiskTier.C, False)}
    got = _saved_days(
        settings,
        monkeypatch,
        students=students,
        extra={"V1": 5000, "R1": 100},
        regular_only=regular_only,
    )
    assert got == days


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
@pytest.mark.parametrize(("extra", "days"), [(500, 1), (501, 2)])
def test_per_student_cap_still_protects_a_visitor_when_on(settings, monkeypatch, extra, days):
    students = {"V1": (RiskTier.C, True)}
    got = _saved_days(
        settings,
        monkeypatch,
        students=students,
        extra={"V1": extra},
        regular_only=True,
        per_student=500,
        total=10**6,
    )
    assert got == days


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
@pytest.mark.parametrize(("extra", "days"), [(100, 1), (101, 2)])
def test_high_risk_visitor_keeps_the_tighter_cap_when_on(settings, monkeypatch, extra, days):
    students = {"V1": (RiskTier.A, True)}
    got = _saved_days(
        settings,
        monkeypatch,
        students=students,
        extra={"V1": extra},
        regular_only=True,
        high_risk=100,
        total=10**6,
    )
    assert got == days


@pytest.mark.django_db
@pytest.mark.usefixtures("_compaction")
def test_regular_students_total_is_still_capped_when_on(settings, monkeypatch):
    # Regular students alone pay 150 + 151 = 301 > 300 -> refused even though the
    # visitor (9000) is excluded from the total.
    students = {"V1": (RiskTier.C, True), "R1": (RiskTier.C, False), "R2": (RiskTier.C, False)}
    got = _saved_days(
        settings,
        monkeypatch,
        students=students,
        extra={"V1": 9000, "R1": 150, "R2": 151},
        regular_only=True,
    )
    assert got == 2


# -- secondary-recovery polish targeting --------------------------------------


@pytest.mark.parametrize(("flag", "expected"), [(OFF, {"A1", "B1", "C1"}), (ON, {"A1", "B1"})])
def test_polish_targets_skip_visitor_gaps_only_when_on(monkeypatch, flag, expected):
    from core.services import timetable_secondary_recovery as sr
    from core.services.timetable_assignment_models import TimetableEvaluationResult

    sections = {
        s.section_id: s
        for s in (
            _section("A1", "A", [(0, 480, 555)]),
            _section("B1", "B", [(0, 720, 795)]),
            _section("C1", "C", [(1, 480, 555)]),
        )
    }
    reg, vis = _state("R1", 90), _state("V1", 90)
    reg.section_ids, vis.section_ids = {"A1", "B1"}, {"C1"}
    evaluation = TimetableEvaluationResult(
        "x", (0, 0, 0, 0, 180, 0, 0, 0, 0), {"R1": reg, "V1": vis}, [], [], [], []
    )
    profiles = {"R1": _profile("R1"), "V1": _profile("V1", visitor=True)}
    seen: list[set[str]] = []

    def capture(_sections, _catalog, targets, _locked):
        seen.append(set(targets))
        return []

    monkeypatch.setattr(sr, "generate_all_repattern_moves", capture)
    monkeypatch.setattr(sr, "generate_swap_moves", lambda *a, **k: [])
    with override_settings(**flag):
        sr.recover_secondary_quality(
            current_eval=evaluation,
            sections_by_id=sections,
            pattern_catalog={},
            student_profiles=profiles,
            course_rigidity={},
            room_evaluator=lambda _s: {"unassigned_count": 0, "unassigned_section_ids": []},
            max_polish_iterations=1,
        )
    assert seen
    assert seen[0] == expected
