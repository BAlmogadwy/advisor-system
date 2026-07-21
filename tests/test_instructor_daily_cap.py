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
