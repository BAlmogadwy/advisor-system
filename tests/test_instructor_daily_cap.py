"""Tests for the hard instructor daily-session cap.

The cap (``TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED`` / ``TIMETABLE_INSTRUCTOR_
DAILY_CAP``) limits how many sessions (lectures AND labs) an instructor may teach
on a single day. It is enforced STRUCTURALLY in the solver generators and is
NEVER part of the lexicographic score tuple — so with the flag off the optimiser
output is byte-identical to before. These tests cover the shared counters, the
side-band evaluator attribute, and the flag-off parity contract.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_pr4_instructor import (
    count_instructor_daily_overloads,
    exceeds_instructor_daily_cap,
    get_instructor_daily_cap,
    is_instructor_daily_cap_enabled,
)


def _section(section_id, course_code, meetings):
    return SectionState(
        section_id=section_id,
        course_code=course_code,
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for d, s, e in meetings],
        max_capacity=30,
        reserve_capacity=0,
    )


def _by_id(*sections):
    return {s.section_id: s for s in sections}


# ── Shared counters ──────────────────────────────────────────────────────────


def test_exceeds_false_at_cap() -> None:
    # Instructor 7 has exactly 3 Sunday sessions across 3 sections — at cap, OK.
    secs = [
        _section("C1_S1", "C1", [(0, 540, 615)]),
        _section("C2_S1", "C2", [(0, 660, 735)]),
        _section("C3_S1", "C3", [(0, 780, 855)]),
    ]
    smap = {"C1_S1": frozenset({7}), "C2_S1": frozenset({7}), "C3_S1": frozenset({7})}
    assert exceeds_instructor_daily_cap(_by_id(*secs), smap, 3) is False
    assert count_instructor_daily_overloads(_by_id(*secs), smap, 3) == 0


def test_exceeds_true_above_cap() -> None:
    # 4 Sunday sessions for instructor 7 -> over a cap of 3.
    secs = [
        _section("C1_S1", "C1", [(0, 540, 615)]),
        _section("C2_S1", "C2", [(0, 660, 735)]),
        _section("C3_S1", "C3", [(0, 780, 855)]),
        _section("C4_S1", "C4", [(0, 900, 975)]),
    ]
    smap = {k: frozenset({7}) for k in ("C1_S1", "C2_S1", "C3_S1", "C4_S1")}
    assert exceeds_instructor_daily_cap(_by_id(*secs), smap, 3) is True
    assert count_instructor_daily_overloads(_by_id(*secs), smap, 3) == 1


def test_labs_count_toward_cap() -> None:
    # 2 lectures + 2 labs (a 4-credit course) on the SAME day = 4 > cap 3.
    secs = [
        _section("C1_S1", "C1", [(1, 540, 615)]),  # lecture
        _section("C1_S2", "C1", [(1, 660, 735)]),  # lecture
        _section("C1_LAB1", "C1", [(1, 780, 880)]),  # lab (100 min)
        _section("C1_LAB2", "C1", [(1, 900, 1000)]),  # lab
    ]
    smap = {k: frozenset({7}) for k in ("C1_S1", "C1_S2", "C1_LAB1", "C1_LAB2")}
    assert exceeds_instructor_daily_cap(_by_id(*secs), smap, 3) is True


def test_different_days_within_cap() -> None:
    # 4 sessions but spread across 2 days (2+2) -> within a cap of 3.
    secs = [
        _section("C1_S1", "C1", [(0, 540, 615)]),
        _section("C2_S1", "C2", [(0, 660, 735)]),
        _section("C3_S1", "C3", [(1, 540, 615)]),
        _section("C4_S1", "C4", [(1, 660, 735)]),
    ]
    smap = {k: frozenset({7}) for k in ("C1_S1", "C2_S1", "C3_S1", "C4_S1")}
    assert exceeds_instructor_daily_cap(_by_id(*secs), smap, 3) is False


def test_two_instructors_independent() -> None:
    # Instructor 7 over cap on SUN; instructor 9 fine. Still flagged.
    secs = [
        _section("C1_S1", "C1", [(0, 540, 615)]),
        _section("C2_S1", "C2", [(0, 660, 735)]),
        _section("C3_S1", "C3", [(0, 780, 855)]),
        _section("C4_S1", "C4", [(0, 900, 975)]),
        _section("D1_S1", "D1", [(0, 540, 615)]),
    ]
    smap = {
        "C1_S1": frozenset({7}),
        "C2_S1": frozenset({7}),
        "C3_S1": frozenset({7}),
        "C4_S1": frozenset({7}),
        "D1_S1": frozenset({9}),
    }
    assert exceeds_instructor_daily_cap(_by_id(*secs), smap, 3) is True
    assert count_instructor_daily_overloads(_by_id(*secs), smap, 3) == 1


def test_empty_map_is_safe() -> None:
    secs = [_section("C1_S1", "C1", [(0, 540, 615)])]
    assert exceeds_instructor_daily_cap(_by_id(*secs), {}, 3) is False
    assert count_instructor_daily_overloads(_by_id(*secs), {}, 3) == 0


def test_configurable_cap_two() -> None:
    secs = [
        _section("C1_S1", "C1", [(0, 540, 615)]),
        _section("C2_S1", "C2", [(0, 660, 735)]),
        _section("C3_S1", "C3", [(0, 780, 855)]),
    ]
    smap = {k: frozenset({7}) for k in ("C1_S1", "C2_S1", "C3_S1")}
    assert exceeds_instructor_daily_cap(_by_id(*secs), smap, 2) is True  # 3 > 2
    assert exceeds_instructor_daily_cap(_by_id(*secs), smap, 3) is False


# ── Flag helpers ─────────────────────────────────────────────────────────────


@override_settings(TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True, TIMETABLE_INSTRUCTOR_DAILY_CAP=5)
def test_flag_helpers_read_settings() -> None:
    assert is_instructor_daily_cap_enabled() is True
    assert get_instructor_daily_cap() == 5


@override_settings(TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=False)
def test_flag_helper_default_off() -> None:
    assert is_instructor_daily_cap_enabled() is False


# ── Side-band evaluator attribute + tuple parity ─────────────────────────────


def _profiles():
    return {
        "S1": StudentProfile(
            student_id="S1",
            department="CS",
            recommended_courses=["C1"],
            risk_tier=RiskTier.A,
            intra_tier_score=9.0,
        ),
    }


def _overloaded_sections():
    return [
        _section("C1_S1", "C1", [(0, 540, 615)]),
        _section("C2_S1", "C2", [(0, 660, 735)]),
        _section("C3_S1", "C3", [(0, 780, 855)]),
        _section("C4_S1", "C4", [(0, 900, 975)]),
    ]


_OVERLOAD_MAP = {k: frozenset({7}) for k in ("C1_S1", "C2_S1", "C3_S1", "C4_S1")}


@pytest.mark.django_db
@override_settings(
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True, TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=False
)
def test_eval_reports_overload_but_keeps_six_tuple() -> None:
    res = evaluate_generated_timetable_candidate(
        candidate_id="c",
        generated_sections=_overloaded_sections(),
        student_profiles=_profiles(),
        course_rigidity={c: 1.0 for c in ("C1", "C2", "C3", "C4")},
        section_instructor_ids=_OVERLOAD_MAP,
    )
    # Side-band attribute sees the overload...
    assert res.instructor_overload_count == 1
    # ...but the lexicographic tuple shape is untouched (cap never shifts it).
    assert len(res.lexicographic_score) == 6


@pytest.mark.django_db
@override_settings(TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=False)
def test_eval_overload_zero_when_flag_off() -> None:
    res = evaluate_generated_timetable_candidate(
        candidate_id="c",
        generated_sections=_overloaded_sections(),
        student_profiles=_profiles(),
        course_rigidity={c: 1.0 for c in ("C1", "C2", "C3", "C4")},
        section_instructor_ids=_OVERLOAD_MAP,
    )
    assert res.instructor_overload_count == 0  # flag off -> side-band stays 0
    assert len(res.lexicographic_score) == 6


# ── Repair pass (DB integration) ─────────────────────────────────────────────


def _overload_board():
    """Build a scenario where one instructor teaches 4 sections all on MON, with
    no students (the structural relocation is what we assert). Returns the
    scenario."""
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
        academic_year="1448", term="1", name="AI M T1 cap", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="T1", nominal_term=1)
    instr = Instructor.objects.create(
        full_name="Dr Cap", normalised_name=normalise_instructor("Dr Cap")
    )
    mon_times = [("09:00", "10:15"), ("10:30", "11:45"), ("13:00", "14:15"), ("14:30", "15:45")]
    for i, (start, end) in enumerate(mon_times, start=1):
        code = f"C{i}"
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
            source_tag="cap_test",
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
        apply_primary_instructor(ts, scenario, board, ts.course_code)  # fan "Dr Cap"
    return scenario


def _max_sessions_per_instructor_day(scenario):
    from collections import defaultdict

    from core.models import SectionPlacement, TermSectionMeeting

    instr = {
        ts: nm.strip()
        for ts, nm in TermSectionMeeting.objects.filter(term_section__scenario=scenario)
        .exclude(instructor="")
        .values_list("term_section_id", "instructor")
    }
    counts: dict = defaultdict(int)
    for p in SectionPlacement.objects.filter(board__scenario=scenario).exclude(day=""):
        nm = instr.get(p.term_section_id)
        if nm:
            counts[(nm, p.day)] += 1
    return max(counts.values()) if counts else 0


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_repair_resolves_existing_overload() -> None:
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads

    scenario = _overload_board()
    assert _max_sessions_per_instructor_day(scenario) == 4  # 4 on MON before

    report = repair_instructor_daily_overloads(scenario.id)

    assert report["enabled"] is True
    assert report["detected"], "repair should detect the MON overload"
    assert _max_sessions_per_instructor_day(scenario) <= 3  # cap satisfied after
    assert report["remaining_violations"] == 0


@pytest.mark.django_db(transaction=True)
@override_settings(TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=False)
def test_repair_noop_when_flag_off() -> None:
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads

    scenario = _overload_board()
    report = repair_instructor_daily_overloads(scenario.id)
    assert report["enabled"] is False
    assert _max_sessions_per_instructor_day(scenario) == 4  # untouched when off


def _overload_board_multimeeting():
    """One instructor teaches TWO courses, two sections each (4 sections), and
    every section has a meeting on THU plus a second meeting on its own quieter
    day — so THU carries 4 sessions (an overload) while MON/TUE/WED/SUN sit light.

    This is the shape that exposed the delete-bias in the wild (Nawaf, scn 644):
    because each section is MULTI-meeting, dropping its THU meeting leaves the
    section otherwise placed and barely moves the student score, whereas
    RELOCATING that meeting to a free day perturbs student day-patterns and costs
    more. A repair that picks the cheaper student score therefore UNPLACES —
    silently dropping a class — when a free day was right there. The single-meeting
    ``_overload_board`` never surfaced this, since dropping its only meeting makes
    a student unresolved (expensive), so relocation always won there.
    """
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
        academic_year="1448", term="1", name="AI M T1 cap mm", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="T1", nominal_term=1)
    instr = Instructor.objects.create(
        full_name="Dr Load", normalised_name=normalise_instructor("Dr Load")
    )
    # 4 sections, each: a THU meeting (the overload) + a second meeting on a light day.
    plan = [
        ("C1", "S1", "09:00", "10:15", "MON"),
        ("C1", "S2", "10:30", "11:45", "TUE"),
        ("C2", "S1", "13:00", "14:15", "WED"),
        ("C2", "S2", "14:30", "15:45", "SUN"),
    ]
    for code, sec, thu_start, thu_end, other_day in plan:
        CourseInstructor.objects.get_or_create(
            program="AI", course_code=code, section="M", instructor=instr, role="primary"
        )
        ts = TermSection.objects.create(
            scenario=scenario,
            course_key=f"{code}{sec}",
            section=sec,
            course_code=code,
            course_number=code,
            course_name=code,
            available_capacity=30,
            source_tag="cap_test",
        )
        for day, start, end in [("THU", thu_start, thu_end), (other_day, thu_start, thu_end)]:
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


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_repair_relocates_rather_than_unplaces_when_a_free_day_exists(monkeypatch) -> None:
    """The registrar rule: an over-cap day is fixed by MOVING a session to a free
    day, never by dropping the class.

    Regression guard for the delete-bias. The bias only appears when relocation
    scores WORSE than unplacement, which needs student demand — so we drive the
    evaluator directly to reproduce exactly that condition (as the real evaluator
    did on scn 644): score = number of the instructor's meetings NOT on the
    overloaded day. Relocating a session off that day raises the count (costlier);
    dropping the meeting leaves it unchanged (cheaper). Under the OLD code, which
    let relocation and unplacement compete on score, unplace wins here and drops a
    class. The fix must relocate regardless, because a free day exists. Verified
    to FAIL against the pre-fix repair.
    """
    from collections import defaultdict

    from core.models import SectionPlacement, TermSection
    from core.services.timetable_instructor_cap_repair import (
        _day_idx,
        repair_instructor_daily_overloads,
    )

    scenario = _overload_board_multimeeting()
    assert _max_sessions_per_instructor_day(scenario) == 4  # 4 on THU before

    thu = _day_idx("THU")

    class _Result:
        def __init__(self, score):
            self.lexicographic_score = score

    def _fake_eval(*, generated_sections, **_kw):
        # Cheaper to DROP a THU meeting (off-THU count unchanged) than to MOVE it
        # to another day (off-THU count rises) — the delete-bias trigger.
        off_thu = sum(1 for s in generated_sections for m in s.meetings if m.day != thu)
        return _Result((0, 0, 0, 0, off_thu, 0))

    monkeypatch.setattr(
        "core.services.timetable_candidate_eval.evaluate_generated_timetable_candidate",
        _fake_eval,
    )
    # profiles must be non-empty for the repair to score candidates at all.
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2.build_student_profiles_for_scenario",
        lambda _sid: {"1": object()},
    )

    before_sections = set(
        TermSection.objects.filter(scenario=scenario).values_list("id", flat=True)
    )

    report = repair_instructor_daily_overloads(scenario.id)

    assert report["enabled"] is True
    assert report["detected"], "should detect the THU overload"
    # The core assertion: nothing was dropped even though unplace scored cheaper.
    assert report["unplaced"] == [], "repair unplaced a class when a free day existed"
    assert report["repaired"], "repair should have relocated at least one session"
    assert _max_sessions_per_instructor_day(scenario) <= 3
    assert report["remaining_violations"] == 0

    # Every section still has at least one placed meeting (none lost entirely).
    placed_by_section: dict[int, int] = defaultdict(int)
    for p in SectionPlacement.objects.filter(board__scenario=scenario).exclude(day=""):
        placed_by_section[p.term_section_id] += 1
    for ts_id in before_sections:
        assert placed_by_section[ts_id] >= 1, f"section {ts_id} lost all meetings"


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_optimise_current_reconciles_meetings_on_the_no_improve_branch(monkeypatch) -> None:
    """When "Improve current board" finds no student-score improvement, the cap
    repair still runs (hoisted out of the improvement branch) — and it must first
    reconcile TermSectionMeeting rows to placements, exactly as the improve branch
    does. The repair's _relocate/_unplace key TSM by the placement's current
    (day, start_time); on a board with pre-existing placement/TSM drift (e.g. a
    prior manual bulk time-move that wrote SectionPlacement but not TSM), skipping
    that reconciliation would move the placement while the meeting row stayed
    stale, so the cap fix would be invisible to the Instructors export and
    validate_placement. Regression guard for the hoist that initially left the
    sync inside the improve branch only.
    """
    from core.models import (
        CourseInstructor,
        DeliveryBoard,
        Instructor,
        ScenarioStudentCourseRequest,
        ScenarioStudentMap,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
    )
    from core.services import timetable_board_persistence as bp
    from core.services.course_instructor_assignment import apply_primary_instructor
    from core.services.timetable_optimizer_v2 import optimise_current_timetable
    from core.services.timetable_pr4_instructor import normalise_instructor

    scenario = _overload_board_multimeeting()
    board = DeliveryBoard.objects.get(scenario=scenario)

    # A separate, already-optimal course with one enrolled student. This makes
    # student profiles non-empty (so optimise-current does NOT early-return) while
    # leaving no student-score improvement to find — the overload sections carry
    # no demand, so relocating them is score-neutral. Result: the NO-IMPROVE
    # branch is taken, which is exactly the path the reconciliation fix is about.
    solo = Instructor.objects.create(
        full_name="Dr Solo", normalised_name=normalise_instructor("Dr Solo")
    )
    CourseInstructor.objects.create(
        program="AI", course_code="C9", section="M", instructor=solo, role="primary"
    )
    ts9 = TermSection.objects.create(
        scenario=scenario,
        course_key="C9",
        section="S1",
        course_code="C9",
        course_number="C9",
        course_name="C9",
        available_capacity=30,
        source_tag="cap_test",
    )
    TermSectionMeeting.objects.create(
        term_section=ts9, day="WED", start_time="08:00", end_time="09:15", room="", instructor=""
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=ts9,
        day="WED",
        start_time="08:00",
        end_time="09:15",
        room="R9",
        is_locked=False,
    )
    apply_primary_instructor(ts9, scenario, board, "C9")
    ScenarioStudentMap.objects.create(
        scenario=scenario, student_id=990001, primary_term=1, recommended_courses=["C9"]
    )
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario,
        student_id=990001,
        course_key="C9",
        course_code="C9",
        primary_term=1,
        status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
        priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
        source="test",
    )

    # Simulate placement/TSM drift on a NON-overloaded section: move its meeting
    # row off where its placement sits, as a stray manual edit would.
    drift_p = SectionPlacement.objects.filter(board__scenario=scenario, day="MON").first()
    assert drift_p is not None
    drift_tsm = TermSectionMeeting.objects.filter(
        term_section_id=drift_p.term_section_id, day="MON"
    ).first()
    drift_tsm.day = "SAT"
    drift_tsm.start_time = "08:00"
    drift_tsm.end_time = "09:15"
    drift_tsm.save()

    calls = {"sync": 0}
    _orig = bp.sync_meetings_from_placements

    def _spy(sid):
        calls["sync"] += 1
        return _orig(sid)

    monkeypatch.setattr(bp, "sync_meetings_from_placements", _spy)

    result = optimise_current_timetable(
        scenario_id=scenario.id,
        max_search_iterations=3,
        run_chain_search=False,
        run_cpsat_polish=False,
    )

    # Confirm we exercised the no-improve branch (the one the fix is about).
    assert result["persist_result"]["action"] == "no_change"
    # The reconciliation ran before the repair despite no persist happening.
    assert calls["sync"] >= 1, "no-improve branch did not reconcile meetings before repair"
    # Cap satisfied, and NO placement/TSM drift remains anywhere on the board.
    assert _max_sessions_per_instructor_day(scenario) <= 3
    for p in SectionPlacement.objects.filter(board__scenario=scenario).exclude(day=""):
        assert TermSectionMeeting.objects.filter(
            term_section_id=p.term_section_id, day=p.day, start_time=p.start_time
        ).exists(), "a TermSectionMeeting row drifted from its placement"


# ── Safety-gate exemption for the mandatory cap repair ───────────────────────


class _FakeSnapshot:
    is_empty = False


_CLEAN_SAFETY = {
    "same_board_conflicts": {"overlaps": 0, "instructors": 0, "rooms": 0},
    "instructor_clashes_scenario": 0,
}
_OVERLAP_SAFETY = {
    "same_board_conflicts": {"overlaps": 1, "instructors": 0, "rooms": 0},
    "instructor_clashes_scenario": 0,
}


def _wire_runner(monkeypatch, summaries, opt_result):
    """Patch run_v2_optimisation_guarded's collaborators so the gate can be tested
    without a real board. ``summaries`` is consumed in call order (before, after,
    and again inside the rollback branch)."""
    from core.services import timetable_optimizer_v2 as opt
    from core.services import timetable_v2_runner as runner

    restored = {"n": 0}
    it = iter(summaries)
    monkeypatch.setattr(runner, "snapshot_scenario", lambda _sid: _FakeSnapshot())
    monkeypatch.setattr(
        runner, "restore_scenario", lambda _sid, _snap: restored.__setitem__("n", restored["n"] + 1)
    )
    monkeypatch.setattr(runner, "compute_scenario_safety_summary", lambda _sid, *a, **k: next(it))
    monkeypatch.setattr(opt, "optimise_current_timetable", lambda **_kw: dict(opt_result))
    return restored


@pytest.mark.django_db
def test_safety_gate_exempts_a_mandatory_repair_overlap(monkeypatch) -> None:
    """The cap is a hard rule that WINS against students, so a repair which accepts
    a warning-level cross-course overlap as the least-harm way to relocate an
    over-cap session must NOT be rolled back by the safety gate — rolling back
    would reinstate the very violation the repair cleared. The gate judges the
    board the OPTIMISER produced (safety_before_instructor_passes), not the
    post-repair board.
    """
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    # before = clean; after = the repair added one warning overlap. A third copy
    # covers the pre-fix code path (which would roll back and re-read the summary),
    # so this guard fails on the clean assertion rather than a StopIteration.
    restored = _wire_runner(
        monkeypatch,
        summaries=[_CLEAN_SAFETY, _OVERLAP_SAFETY, _OVERLAP_SAFETY],
        opt_result={
            "final_score": [0, 0, 0, 0, 0, 0],
            "baseline_score": [0, 0, 0, 0, 0, 0],
            "safety_before_instructor_passes": _CLEAN_SAFETY,  # optimiser's board was clean
        },
    )

    result = run_v2_optimisation_guarded(123, mode="current")

    assert result["safety_blocked"] is False, "mandatory repair overlap wrongly rolled back"
    assert restored["n"] == 0


@pytest.mark.django_db
def test_safety_gate_still_blocks_an_optimiser_introduced_overlap(monkeypatch) -> None:
    """The exemption must not blind the gate to a real regression: when the overlap
    is present in the optimiser's OWN board (no safety_before_instructor_passes, or
    it already shows the overlap), the gate still blocks and rolls back."""
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    # before = clean; after = overlap; rollback branch reads the summary once more.
    restored = _wire_runner(
        monkeypatch,
        summaries=[_CLEAN_SAFETY, _OVERLAP_SAFETY, _OVERLAP_SAFETY],
        opt_result={
            "final_score": [0, 0, 0, 0, 0, 0],
            "baseline_score": [0, 0, 0, 0, 0, 0],
            # no safety_before_instructor_passes → gate falls back to the real
            # post-run summary, which carries the optimiser's own overlap.
        },
    )

    result = run_v2_optimisation_guarded(123, mode="current")

    assert result["safety_blocked"] is True, "a genuine optimiser regression slipped past the gate"
    assert restored["n"] == 1
