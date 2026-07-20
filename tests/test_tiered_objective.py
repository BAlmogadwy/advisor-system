"""Tiered lexicographic objective — ON/OFF behaviour, flag-off byte parity,
Tier-2 tolerance, high-risk override, and layout-aware accessors."""

from __future__ import annotations

from django.test import override_settings

from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
    StudentProfile,
    UnresolvedReason,
)
from core.services.timetable_flags import (
    get_tiered_t2_tolerance,
    is_tiered_objective_enabled,
)
from core.services.timetable_student_assignment import (
    TI_SOFT,
    TI_SPREAD,
    TI_STUDENT_COST,
    TI_T1,
    TI_T2_OVER,
    decode_score,
    evaluate_assignability_lexicographic,
    instructor_idle_of,
    is_tiered_score,
    reserve_used_of,
    strip_instructor_idle,
)

# ── fixture builders ─────────────────────────────────────────────────────


def _profile(sid: str, tier: RiskTier = RiskTier.C) -> StudentProfile:
    return StudentProfile(
        student_id=sid, department="X", recommended_courses=[], risk_tier=tier, intra_tier_score=1.0
    )


def _state(sid: str, unresolved: list[str], gap: int = 0) -> StudentAssignmentState:
    st = StudentAssignmentState(student_id=sid)
    st.total_gap_minutes = gap
    st.unresolved_courses = {c: UnresolvedReason(course_code=c, reason="full") for c in unresolved}
    return st


def _run(states, profiles, sections=None, *, tiers, enabled):
    with override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=enabled):
        return evaluate_assignability_lexicographic(states, profiles, sections or {}, None, tiers)


# ── flag + parity ────────────────────────────────────────────────────────


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=False)
def test_flag_default_off() -> None:
    assert is_tiered_objective_enabled() is False


def test_tolerance_reader_default_and_clamp() -> None:
    with override_settings(TIMETABLE_TIERED_T2_TOLERANCE=3):
        assert get_tiered_t2_tolerance() == 3
    with override_settings(TIMETABLE_TIERED_T2_TOLERANCE=-4):
        assert get_tiered_t2_tolerance() == 0  # negatives floored


def test_flag_off_byte_parity_with_and_without_map() -> None:
    profiles = {"S1": _profile("S1", RiskTier.A), "S2": _profile("S2")}
    states = {"S1": _state("S1", ["AI100"], gap=120), "S2": _state("S2", [], gap=45)}
    tiers = {"AI100": "T1"}

    off_none = _run(states, profiles, tiers=None, enabled=False)
    off_map = _run(states, profiles, tiers=tiers, enabled=False)
    on_none = _run(states, profiles, tiers=None, enabled=True)  # no map => legacy fallback

    assert off_none == off_map == on_none
    assert len(off_none) == 6
    # legacy: (tier_a, unres_students, unassigned, clashes, gap, reserve)
    assert off_none == (1, 1, 1, 0, 165, 0)


def test_flag_on_produces_nine_tuple() -> None:
    profiles = {"S1": _profile("S1")}
    states = {"S1": _state("S1", ["AI100"])}
    score = _run(states, profiles, tiers={"AI100": "T1"}, enabled=True)
    assert len(score) == 9
    assert is_tiered_score(score)


# ── tier decomposition + tolerance + high-risk override ──────────────────


def _decomposition_fixture():
    """2 T1-unresolved, 5 T2-unresolved in one course, 4 T3-unresolved.
    High-risk (A) students: one in T1, one in T2, one in T3-only."""
    tiers = {"AI100": "T1", "CS500": "T2", "GS100": "T3"}
    profiles: dict[str, StudentProfile] = {}
    states: dict[str, StudentAssignmentState] = {}

    def add(sid, course, tier=RiskTier.C):
        profiles[sid] = _profile(sid, tier)
        states[sid] = _state(sid, [course])

    add("S1", "AI100", RiskTier.A)  # high-risk T1
    add("S2", "AI100")
    add("S3", "CS500", RiskTier.A)  # high-risk T2
    add("S4", "CS500")
    add("S5", "CS500")
    add("S6", "CS500")
    add("S7", "CS500")
    add("S8", "GS100", RiskTier.A)  # high-risk but T3 only -> NOT counted
    add("S9", "GS100")
    add("S10", "GS100")
    add("S11", "GS100")
    return states, profiles, tiers


@override_settings(TIMETABLE_TIERED_T2_TOLERANCE=3, TIMETABLE_TIERED_SOFT_GAP_BUDGET=0)
def test_tier_decomposition_and_tolerance() -> None:
    # Budget 0 isolates the tier counting (position 4 == pure real gap == 0).
    states, profiles, tiers = _decomposition_fixture()
    score = _run(states, profiles, tiers=tiers, enabled=True)
    # (highrisk, clash, t1, t2_over, cost, soft, reserve, spread, idle)
    # t1 = 2; t2 course has 5 unresolved, tol 3 => over 2, within 3;
    # soft = t3(4) + t2_within(3) = 7; highrisk = S1(T1)+S3(T2) = 2 (S8 excluded)
    assert score == (2, 0, 2, 2, 0, 7, 0, 0, 0)


@override_settings(TIMETABLE_TIERED_T2_TOLERANCE=5)
def test_tolerance_is_configurable() -> None:
    states, profiles, tiers = _decomposition_fixture()
    score = _run(states, profiles, tiers=tiers, enabled=True)
    # tol 5 now fully absorbs the 5 T2 unresolved: over 0, within 5.
    assert score[TI_T2_OVER] == 0
    assert score[TI_SOFT] == 4 + 5  # t3 + t2_within


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=False)
def test_legacy_tier_a_counts_all_tiers_unlike_tiered_highrisk() -> None:
    # Legacy position 0 counts ANY high-risk unresolved (incl. the T3-only S8);
    # tiered position 0 excludes it. Proves the override is tier-scoped.
    states, profiles, tiers = _decomposition_fixture()
    legacy = evaluate_assignability_lexicographic(states, profiles, {}, None, None)
    assert legacy[0] == 3  # S1, S3, S8


# ── discriminating regressions (one per boundary) ────────────────────────


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True, TIMETABLE_TIERED_T2_TOLERANCE=3)
def test_t1_dominates_soft() -> None:
    # Leaving ONE T1 core unresolved must rank worse than 50 T3 electives.
    tiers = {"AI100": "T1", "GS100": "T3"}
    x_states = {"X": _state("X", ["AI100"])}
    y_states = {f"Y{i}": _state(f"Y{i}", ["GS100"]) for i in range(50)}
    x_profiles = {"X": _profile("X")}
    y_profiles = {f"Y{i}": _profile(f"Y{i}") for i in range(50)}

    x = _run(x_states, x_profiles, tiers=tiers, enabled=True)
    y = _run(y_states, y_profiles, tiers=tiers, enabled=True)
    assert y < x  # y (50 T3) is preferred over x (1 T1)
    assert x[TI_T1] == 1 and y[TI_SOFT] == 50


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True, TIMETABLE_TIERED_T2_TOLERANCE=3)
def test_t2_tolerance_knife_edge() -> None:
    tiers = {"CS500": "T2"}

    def t2_score(n):
        states = {f"S{i}": _state(f"S{i}", ["CS500"]) for i in range(n)}
        profiles = {f"S{i}": _profile(f"S{i}") for i in range(n)}
        return _run(states, profiles, tiers=tiers, enabled=True)

    at_tol = t2_score(3)
    over_tol = t2_score(4)
    assert at_tol[TI_T2_OVER] == 0 and at_tol[TI_SOFT] == 3
    assert over_tol[TI_T2_OVER] == 1 and over_tol[TI_SOFT] == 3
    # crossing tolerance flips the candidate from soft to near-hard: worse.
    assert over_tol > at_tol


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_high_risk_t3_only_does_not_raise_position_zero() -> None:
    tiers = {"GS100": "T3", "AI100": "T1"}
    a_t3 = {"A": _state("A", ["GS100"])}
    a_t3_prof = {"A": _profile("A", RiskTier.A)}
    a_t1 = {"A": _state("A", ["AI100"])}
    a_t1_prof = {"A": _profile("A", RiskTier.A)}

    s_t3 = _run(a_t3, a_t3_prof, tiers=tiers, enabled=True)
    s_t1 = _run(a_t1, a_t1_prof, tiers=tiers, enabled=True)
    assert s_t3[0] == 0  # T3-only high-risk: no override
    assert s_t1[0] == 1  # T1 high-risk: override fires


# ── spread unbundling ────────────────────────────────────────────────────


def _section(sid: str, course: str, meetings: list[tuple[int, int, int]]) -> SectionState:
    return SectionState(
        section_id=sid,
        course_code=course,
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for d, s, e in meetings],
        max_capacity=30,
        reserve_capacity=0,
    )


def test_spread_unbundled_from_gap() -> None:
    # Two sections of one course on DIFFERENT days => a same-course spread
    # penalty. Tiered keeps it in its own slot; legacy folds it into gap.
    sections = {
        "C1-A": _section("C1-A", "C1", [(0, 540, 615)]),
        "C1-B": _section("C1-B", "C1", [(2, 540, 615)]),
    }
    profiles = {"S1": _profile("S1")}
    states = {"S1": _state("S1", [], gap=300)}

    on = _run(states, profiles, sections, tiers={"C1": "T2"}, enabled=True)
    off = _run(states, profiles, sections, tiers=None, enabled=False)

    assert on[TI_SPREAD] > 0
    # No unresolved => soft is 0, so the blended student-cost is pure real gap.
    assert on[TI_SOFT] == 0
    assert on[TI_STUDENT_COST] == 300  # real student gap only (soft term is 0)
    # legacy folds gap and spread together
    assert off[4] == on[TI_STUDENT_COST] + on[TI_SPREAD]


# ── accessors / decode ───────────────────────────────────────────────────


def test_accessors_layout_aware() -> None:
    legacy6 = (0, 1, 2, 3, 4000, 5)
    legacy7 = (0, 1, 2, 3, 4000, 5, 99)
    tiered9 = (0, 1, 2, 3, 4000, 5, 6, 700, 88)

    assert reserve_used_of(legacy6) == 5 and reserve_used_of(tiered9) == 6
    assert instructor_idle_of(legacy7) == 99 and instructor_idle_of(tiered9) == 88
    assert instructor_idle_of(legacy6) == 0 and instructor_idle_of(None) == 0
    assert strip_instructor_idle(legacy7) == (0, 1, 2, 3, 4000, 5)
    assert strip_instructor_idle(tiered9) == (0, 1, 2, 3, 4000, 5, 6, 700)

    dl = decode_score(legacy6)
    assert dl["layout"] == "legacy" and dl["gap_minutes"] == 4000 and dl["reserve_used"] == 5
    dt = decode_score(tiered9)
    assert dt["layout"] == "tiered" and dt["reserve_used"] == 6 and dt["same_course_spread"] == 700
    assert dt["unresolved_courses"] == 2 + 3 + 5  # t1 + t2_over + soft aliases


# ── v2 safety-gate layout awareness ──────────────────────────────────────


def test_student_outcome_gate_labels_are_layout_aware() -> None:
    from core.services.timetable_v2_runner import optimiser_student_outcome_regression

    # Legacy: idx1 rise is labelled unresolved_students.
    legacy = optimiser_student_outcome_regression(
        {"baseline_score": [0, 5, 10, 0, 100, 3], "final_score": [0, 8, 10, 0, 100, 3]}
    )
    assert legacy["blocked"]
    assert legacy["regressions"][0]["metric"] == "unresolved_students"

    # Tiered: idx2 (T1) rise is labelled t1_unresolved, NOT unassigned_courses.
    base = [0, 0, 3, 2, 5000, 7, 3, 200, 0]
    t1_rose = [0, 0, 6, 2, 5000, 7, 3, 200, 0]
    tiered = optimiser_student_outcome_regression({"baseline_score": base, "final_score": t1_rose})
    assert tiered["blocked"]
    assert tiered["regressions"][0]["metric"] == "t1_unresolved"


def test_student_outcome_gate_ignores_soft_tier_regression() -> None:
    from core.services.timetable_v2_runner import optimiser_student_outcome_regression

    # A soft-tier (idx5) rise is deprioritised by policy — must NOT be gated.
    base = [0, 0, 3, 2, 5000, 7, 3, 200, 0]
    soft_rose = [0, 0, 3, 2, 5000, 20, 3, 200, 0]
    res = optimiser_student_outcome_regression({"baseline_score": base, "final_score": soft_rose})
    assert res["blocked"] is False


# ── bounded trade (gap vs gen-ed budget) ─────────────────────────────────


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True, TIMETABLE_TIERED_SOFT_GAP_BUDGET=120)
def test_student_cost_blends_gap_and_soft() -> None:
    # Position 4 is real_gap + budget * soft. One T3 unresolved (soft 1), gap 50.
    tiers = {"GS100": "T3"}
    states = {"S1": _state("S1", ["GS100"], gap=50)}
    profiles = {"S1": _profile("S1")}
    score = _run(states, profiles, tiers=tiers, enabled=True)
    assert score[TI_SOFT] == 1
    assert score[TI_STUDENT_COST] == 50 + 120 * 1  # blended
    # decode recovers the pure real gap and exposes the blended cost
    assert decode_score(score)["gap_minutes"] == 50
    assert decode_score(score)["student_cost"] == 170


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True, TIMETABLE_TIERED_SOFT_GAP_BUDGET=120)
def test_bounded_trade_seats_cheap_genered_drops_expensive() -> None:
    tiers = {"GS100": "T3"}
    # Board A: leaves the gen-ed unresolved, zero gap.  cost = 0 + 120*1 = 120
    a = _run({"S": _state("S", ["GS100"], gap=0)}, {"S": _profile("S")}, tiers=tiers, enabled=True)
    # Board B: seats it at a cheap 50-min gap.          cost = 50 + 120*0 = 50
    b_cheap = _run({"S": _state("S", [], gap=50)}, {"S": _profile("S")}, tiers=tiers, enabled=True)
    # Board C: seats it at an expensive 200-min gap.    cost = 200 + 0 = 200
    c_expensive = _run(
        {"S": _state("S", [], gap=200)}, {"S": _profile("S")}, tiers=tiers, enabled=True
    )
    assert b_cheap < a  # seat the gen-ed when cheap (50 < 120 budget)
    assert a < c_expensive  # leave it unresolved when seating is expensive (200 > 120)


# ── tier-aware seating order ─────────────────────────────────────────────


def _one_slot_pair():
    """Two single-section courses at the SAME slot — only one can be seated.

    Named so the legacy alphabetical tiebreak puts the Tier-2 course first.
    """
    from core.services.timetable_student_assignment import (
        build_sections_by_course,
        build_sections_by_id,
    )

    secs = [
        _section("AAA100-A", "AAA100", [(0, 540, 615)]),  # Tier-2 service course
        _section("ZZZ900-A", "ZZZ900", [(0, 540, 615)]),  # Tier-1 core course
    ]
    sbi = build_sections_by_id(secs)
    return sbi, build_sections_by_course(sbi)


def _one_student():
    return {
        "S1": StudentProfile(
            student_id="S1",
            department="X",
            recommended_courses=["AAA100", "ZZZ900"],
            risk_tier=RiskTier.C,
            intra_tier_score=1.0,
        )
    }


def test_assignment_without_tier_map_keeps_legacy_order() -> None:
    from core.services.timetable_student_assignment import assign_students_to_sections

    sbi, sbc = _one_slot_pair()
    states, _ = assign_students_to_sections(_one_student(), sbi, sbc, {})
    # legacy: alphabetical tiebreak seats AAA100 and strands the core course
    assert "ZZZ900" in states["S1"].unresolved_courses
    assert "AAA100" not in states["S1"].unresolved_courses


def test_assignment_seats_tier1_before_tier2() -> None:
    from core.services.timetable_student_assignment import assign_students_to_sections

    sbi, sbc = _one_slot_pair()
    tiers = {"AAA100": "T2", "ZZZ900": "T1"}
    states, _ = assign_students_to_sections(_one_student(), sbi, sbc, {}, course_tiers=tiers)
    # tier-aware: the core course wins the slot; the service course yields
    assert "ZZZ900" not in states["S1"].unresolved_courses
    assert "AAA100" in states["S1"].unresolved_courses


def _evict_fixture():
    """Core course has ONE section; gen-ed has one section clashing with it, plus
    the core course has an alternative section elsewhere. The repair swap would
    evict core to seat gen-ed unless the tier guard blocks it."""
    from core.services.timetable_student_assignment import (
        build_sections_by_course,
        build_sections_by_id,
    )

    secs = [
        _section("CORE-A", "CORE", [(0, 540, 615)]),  # T1, clashes with GENED-A
        _section("CORE-B", "CORE", [(1, 540, 615)]),  # T1 alternative
        _section("GENED-A", "GENED", [(0, 540, 615)]),  # T3, only option
    ]
    sbi = build_sections_by_id(secs)
    return sbi, build_sections_by_course(sbi)


def _evict_student():
    return {
        "S1": StudentProfile(
            student_id="S1",
            department="X",
            recommended_courses=["CORE", "GENED"],
            risk_tier=RiskTier.C,
            intra_tier_score=1.0,
        )
    }


def test_repair_never_costs_core_its_seat() -> None:
    """Repair may MOVE a Tier-1 course to another section to seat a lower tier,
    but must never leave it unresolved.

    The swap only commits when the displaced course finds an alternative, so
    core keeps a seat either way. (A hard tier guard on eviction was tried and
    reverted: it forfeited ~20 Tier-2 seats on scn 642 to protect a mere section
    choice, which costs only gap-minutes — a strictly worse trade.)
    """
    from core.services.timetable_student_assignment import assign_students_to_sections

    sbi, sbc = _evict_fixture()
    tiers = {"CORE": "T1", "GENED": "T3"}
    states, _ = assign_students_to_sections(_evict_student(), sbi, sbc, {}, course_tiers=tiers)
    st = states["S1"]
    assert "CORE" not in st.unresolved_courses
    assert "CORE" in st.assigned_sections


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True, TIMETABLE_TIERED_SOFT_GAP_BUDGET=0)
def test_zero_budget_is_strict_quality_first() -> None:
    # Budget 0 => soft never justifies any gap; position 4 == pure real gap.
    tiers = {"GS100": "T3"}
    states = {"S1": _state("S1", ["GS100"], gap=999)}
    score = _run(states, {"S1": _profile("S1")}, tiers=tiers, enabled=True)
    assert score[TI_STUDENT_COST] == 999  # no soft contribution
