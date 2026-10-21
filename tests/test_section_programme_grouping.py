"""Grouping sections by programme never costs a seat (owner wish 2026-10-21).

A section marked "mainly for programme X" takes X's students first and other
programmes prefer the course's other sections. The pass runs after the seating
on a FINISHED board and only re-sections students who are already seated, so:

* R1 — no mark, no pass: the frozen seating is returned untouched;
* R2 — on every board, with marks, every student keeps the same courses and the
  same unresolved reasons, nobody gains a clash, reserve use does not rise and
  nothing passes capacity (a property test over the anonymised real boards and
  random synthetic ones, both score layouts) — and the guard never has to fire;
* R3 — the marked programme fills its section, the overflow goes elsewhere and
  is never unseated; coupled courses move together; full sections swap;
* R4 — among equally grouped choices the smaller gap wins, and the gap change
  is reported.
"""

from __future__ import annotations

import random
from copy import deepcopy
from dataclasses import replace

import pytest
from django.test import override_settings

from core.services import timetable_student_assignment as ssa
from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from tests.test_student_assignment_parity import (
    FLAG_COMBOS,
    _perturbed_real_case,
    _sections_sig,
    _states_sig,
    _synthetic_case,
)

PROGRAMMES = ["AI", "AI2", "DS", "DS2", ""]


# ── helpers ──────────────────────────────────────────────────────────────


def _section(sid, course, meetings, cap, reserve=0, mark="", enrolled=0):
    return SectionState(
        section_id=sid,
        course_code=course,
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for d, s, e in meetings],
        max_capacity=cap,
        reserve_capacity=reserve,
        current_enrollment=enrolled,
        enrolled_student_ids={f"pre{sid}{i}" for i in range(enrolled)},
        preferred_programme=mark,
    )


def _student(sid, programme, courses, tier=1, score=0.0):
    return StudentProfile(
        student_id=sid,
        department=programme,
        recommended_courses=list(courses),
        risk_tier=RiskTier(tier),
        intra_tier_score=score,
    )


def _seat(sections, profiles, rigidity=None, tiers=None):
    """Seat a private copy of ``sections``; returns (states, by_id, report)."""
    by_id = ssa.build_sections_by_id(deepcopy(sections))
    report: dict = {}
    states, _unresolved = ssa.assign_students_to_sections(
        profiles,
        by_id,
        ssa.build_sections_by_course(by_id),
        rigidity or {},
        course_tiers=tiers,
        grouping_report=report,
    )
    return states, by_id, report


def _unmarked(sections):
    return [replace(s, preferred_programme="") for s in sections]


def _who(states, profiles, section_id):
    return sorted(
        profiles[sid].department
        for sid, st in states.items()
        if section_id in st.assigned_sections.values()
    )


def _pairs(state, by_id):
    return ssa._meeting_pairs(
        [[(m.day, m.mask) for m in by_id[x].meetings] for x in state.section_ids]
    )


def _grouping_cost(states, profiles, by_id):
    """The board's grouping cost, recomputed from scratch (independent of the pass)."""
    marked = {s.course_code for s in by_id.values() if s.preferred_programme}
    own = {
        c: {s.preferred_programme for s in by_id.values() if s.course_code == c} - {""}
        for c in marked
    }
    total = 0
    for sid, st in states.items():
        programme = profiles[sid].department
        for course, section_id in st.assigned_sections.items():
            if course in marked:
                total += ssa.programme_grouping_cost(
                    programme,
                    by_id[section_id].preferred_programme,
                    bool(programme) and programme in own[course],
                )
    return total


# ── R1: no mark, no pass ─────────────────────────────────────────────────


@override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=True)
def test_with_no_mark_the_pass_is_never_entered(monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("the grouping pass ran on a board with no mark")

    monkeypatch.setattr(ssa, "_group_by_programme", refuse)
    case = _synthetic_case(7)
    result = evaluate_generated_timetable_candidate(
        candidate_id="c",
        generated_sections=case.sections,
        student_profiles=case.profiles,
        course_rigidity=case.rigidity,
    )
    assert "programme_grouping" not in result.quality_score


def test_a_mark_enters_the_pass_and_reports_beside_the_score() -> None:
    sections = [
        _section("C_S1", "C", [(0, 540, 615)], 10),
        _section("C_S2", "C", [(1, 540, 615)], 10, mark="AI2"),
    ]
    profiles = {f"s{i}": _student(f"s{i}", "AI2", ["C"]) for i in range(4)}
    result = evaluate_generated_timetable_candidate(
        candidate_id="c",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity={},
    )
    report = result.quality_score["programme_grouping"]
    assert report["marked_sections"] == ["C_S2"]
    assert report["marked_courses"] == ["C"]
    assert report["grouping_cost_after"] == 0 < report["grouping_cost_before"]
    assert report["guard_reverted"] is False
    # The caller's sections are never touched (the evaluation seats a copy).
    assert [s.current_enrollment for s in sections] == [0, 0]


# ── R2: never a seat, on every board ─────────────────────────────────────


def _marked_case(case, seed):
    rng = random.Random(seed)
    profiles = {
        sid: replace(p, department=rng.choice(PROGRAMMES)) for sid, p in case.profiles.items()
    }
    by_course: dict[str, list[SectionState]] = {}
    for s in case.sections:
        by_course.setdefault(s.course_code, []).append(s)
    multi = sorted(c for c, rows in by_course.items() if len(rows) > 1)
    marked_courses = set(rng.sample(multi, min(len(multi), rng.randint(1, 3)))) if multi else set()
    sections = []
    for s in case.sections:
        mark = ""
        if s.course_code in marked_courses and rng.random() < 0.6:
            mark = rng.choice(PROGRAMMES[:4])
        sections.append(replace(s, preferred_programme=mark))
    if marked_courses and not any(s.preferred_programme for s in sections):
        first = next(i for i, s in enumerate(sections) if s.course_code in marked_courses)
        sections[first] = replace(sections[first], preferred_programme="AI2")
    return sections, profiles


def _hard(score):
    """(positions that must be EQUAL, clashes, reserve) for either layout."""
    if ssa.is_tiered_score(score):
        return (score[0], score[2], score[3], score[5], score[7], score[8]), score[1], score[6]
    return (score[0], score[1], score[2], *score[6:]), score[3], score[5]


def _assert_never_a_seat(case, seed):
    flags = FLAG_COMBOS[seed % len(FLAG_COMBOS)]
    tiers = case.tiers if seed % 3 else None
    sections, profiles = _marked_case(case, seed)
    if not any(s.preferred_programme for s in sections):
        pytest.skip("no multi-section course to mark")
    with override_settings(**flags):
        plain, plain_by_id, plain_report = _seat(
            _unmarked(sections), profiles, case.rigidity, tiers
        )
        grouped, by_id, report = _seat(sections, profiles, case.rigidity, tiers)
        plain_score = ssa.evaluate_assignability_lexicographic(
            plain, profiles, plain_by_id, case.instructors, tiers
        )
        score = ssa.evaluate_assignability_lexicographic(
            grouped, profiles, by_id, case.instructors, tiers
        )
    assert plain_report == {}
    assert report["guard_reverted"] is False, "the guard had to undo a step"
    for sid in profiles:
        assert grouped[sid].unresolved_courses == plain[sid].unresolved_courses, sid
        assert set(grouped[sid].assigned_sections) == set(plain[sid].assigned_sections), sid
        assert _pairs(grouped[sid], by_id) <= _pairs(plain[sid], plain_by_id), sid
    equal, clashes, reserve = _hard(score)
    plain_equal, plain_clashes, plain_reserve = _hard(plain_score)
    assert equal == plain_equal
    assert clashes <= plain_clashes
    assert reserve <= plain_reserve
    for section_id, section in by_id.items():
        assert section.current_enrollment <= section.max_capacity, section_id
        pre = {x for x in section.enrolled_student_ids if x.startswith("pre")}
        assert pre == {
            x for x in plain_by_id[section_id].enrolled_student_ids if x.startswith("pre")
        }
    marked_by_id = {s.section_id: s for s in sections}
    plain_marked = {
        k: replace(v, preferred_programme=marked_by_id[k].preferred_programme)
        for k, v in plain_by_id.items()
    }
    assert _grouping_cost(grouped, profiles, by_id) <= _grouping_cost(plain, profiles, plain_marked)
    assert report["grouping_cost_after"] <= report["grouping_cost_before"]


@pytest.mark.parametrize("key", ["663", "666"])
@pytest.mark.parametrize("seed", range(10))
def test_grouping_never_costs_a_seat_on_the_real_boards(key, seed) -> None:
    _assert_never_a_seat(_perturbed_real_case(key, seed), seed)


@pytest.mark.parametrize("seed", range(60))
def test_grouping_never_costs_a_seat_on_synthetic_boards(seed) -> None:
    _assert_never_a_seat(_synthetic_case(seed), seed)


# ── R3: the marked programme fills its section ──────────────────────────


def _two_sections(cap=25, reserve=0, mark2="AI2", mark1=""):
    return [
        _section("AI236_S1", "AI236", [(0, 540, 615), (2, 540, 615)], cap, reserve, mark1),
        _section("AI236_S2", "AI236", [(1, 540, 615), (3, 540, 615)], cap, reserve, mark2),
    ]


@pytest.mark.parametrize(("ai2", "others"), [(25, 12), (26, 11)])
def test_the_marked_programme_fills_its_section_and_the_overflow_goes_elsewhere(ai2, others):
    profiles = {f"a{i:02d}": _student(f"a{i:02d}", "AI2", ["AI236"]) for i in range(ai2)}
    profiles |= {f"o{i:02d}": _student(f"o{i:02d}", "DS", ["AI236"]) for i in range(others)}
    plain, _by, _r = _seat(_unmarked(_two_sections()), profiles)
    states, by_id, report = _seat(_two_sections(), profiles)
    assert report["guard_reverted"] is False
    assert _who(states, profiles, "AI236_S2") == ["AI2"] * 25
    assert _who(states, profiles, "AI236_S1") == ["AI2"] * (ai2 - 25) + ["DS"] * others
    assert all(not st.unresolved_courses for st in states.values())
    assert [sid for sid, st in states.items() if st.unresolved_courses] == [
        sid for sid, st in plain.items() if st.unresolved_courses
    ]


def test_a_shared_course_puts_the_marked_programme_first_and_others_after():
    sections = [
        _section("ENGL214_S1", "ENGL214", [(0, 540, 615)], 50, 0, "AI2"),
        _section("ENGL214_S2", "ENGL214", [(1, 540, 615)], 50, 0, ""),
    ]
    profiles = {}
    for programme, count in (("AI2", 25), ("AI", 20), ("DS", 20), ("DS2", 20)):
        for i in range(count):
            sid = f"{programme}-{i:02d}"
            profiles[sid] = _student(sid, programme, ["ENGL214"])
    states, _by_id, report = _seat(sections, profiles)
    assert report["guard_reverted"] is False
    first = _who(states, profiles, "ENGL214_S1")
    second = _who(states, profiles, "ENGL214_S2")
    assert first.count("AI2") == 25 and "AI2" not in second
    assert len(second) == 50 and len(first) == 35  # S2 full; the overflow joins S1


def test_coupled_courses_move_together():
    """AI236 and IS124 cross: S1 of one meets when S2 of the other does, so no
    single move is clash-free — only both at once."""
    x, y = (0, 540, 615), (1, 540, 615)
    sections = [
        _section("AI236_S1", "AI236", [x], 100),
        _section("AI236_S2", "AI236", [y], 25, mark="AI2"),
        _section("IS124_S1", "IS124", [y], 100),
        _section("IS124_S2", "IS124", [x], 25, mark="AI2"),
    ]
    profiles = {f"a{i}": _student(f"a{i}", "AI2", ["AI236", "IS124"]) for i in range(4)}
    plain, _by, _r = _seat(_unmarked(sections), profiles)
    assert {tuple(sorted(st.section_ids)) for st in plain.values()} == {("AI236_S1", "IS124_S1")}
    states, by_id, report = _seat(sections, profiles)
    assert report["guard_reverted"] is False
    assert {tuple(sorted(st.section_ids)) for st in states.values()} == {("AI236_S2", "IS124_S2")}
    assert all(_pairs(st, by_id) == 0 for st in states.values())


def test_full_sections_swap_students_to_group_them():
    profiles = {f"a{i:02d}": _student(f"a{i:02d}", "AI2", ["AI236"]) for i in range(25)}
    profiles |= {f"d{i:02d}": _student(f"d{i:02d}", "DS", ["AI236"]) for i in range(25)}
    plain, _by, _r = _seat(_unmarked(_two_sections(mark2="", mark1="AI2")), profiles)
    assert _who(plain, profiles, "AI236_S1") != ["AI2"] * 25, "the plain seating must mix"
    states, by_id, report = _seat(_two_sections(mark2="", mark1="AI2"), profiles)
    assert report["guard_reverted"] is False
    assert _who(states, profiles, "AI236_S1") == ["AI2"] * 25
    assert _who(states, profiles, "AI236_S2") == ["DS"] * 25
    assert [by_id[s].current_enrollment for s in ("AI236_S1", "AI236_S2")] == [25, 25]
    assert report["swaps"] > 0


# ── reserve seats ────────────────────────────────────────────────────────


def test_grouping_never_takes_a_reserve_seat():
    """S2 (marked AI2) has only its 2 reserve seats left: nobody moves in."""
    sections = [
        _section("AI236_S1", "AI236", [(0, 540, 615)], 25, 2),
        _section("AI236_S2", "AI236", [(1, 540, 615)], 25, 2, mark="AI2", enrolled=23),
    ]
    profiles = {f"a{i}": _student(f"a{i}", "AI2", ["AI236"]) for i in range(5)}
    states, by_id, report = _seat(sections, profiles)
    assert report["guard_reverted"] is False
    assert by_id["AI236_S2"].current_enrollment == 23
    assert _who(states, profiles, "AI236_S1") == ["AI2"] * 5
    assert report["moves"] == report["swaps"] == 0


def test_leaving_a_section_over_its_regular_limit_may_take_a_reserve_seat():
    """Net zero: one student leaves S1's reserve and takes S2's — and only one."""
    sections = [
        _section("AI236_S1", "AI236", [(0, 540, 615)], 25, 2, enrolled=20),
        _section("AI236_S2", "AI236", [(1, 540, 615)], 25, 2, mark="AI2", enrolled=23),
    ]
    profiles = {f"a{i}": _student(f"a{i}", "AI2", ["AI236"]) for i in range(4)}
    plain, plain_by_id, _r = _seat(_unmarked(sections), profiles)
    assert [plain_by_id[s].current_enrollment for s in ("AI236_S1", "AI236_S2")] == [24, 23]
    states, by_id, report = _seat(sections, profiles)
    assert report["guard_reverted"] is False
    assert [by_id[s].current_enrollment for s in ("AI236_S1", "AI236_S2")] == [23, 24]
    assert sum(s.reserve_used() for s in by_id.values()) == sum(
        s.reserve_used() for s in plain_by_id.values()
    )
    assert report["moves"] == 1


# ── R4: the smaller gap wins, and the trade is reported ─────────────────


def test_among_equally_grouped_sections_the_smaller_gap_wins_and_is_reported():
    sections = [
        _section("O_S1", "O", [(0, 540, 615)], 10),
        _section("C_S1", "C", [(0, 630, 705)], 10, mark="DS"),  # 15 min after O
        _section("C_S2", "C", [(0, 960, 1035)], 10),  # 345 min after O
        _section("C_S3", "C", [(0, 780, 855)], 10),  # 165 min after O
    ]
    profiles = {"s1": _student("s1", "AI2", ["O", "C"])}
    plain, _by, _r = _seat(_unmarked(sections), profiles)
    assert plain["s1"].assigned_sections["C"] == "C_S1"
    assert plain["s1"].total_gap_minutes == 15
    states, _by_id, report = _seat(sections, profiles)
    assert states["s1"].assigned_sections["C"] == "C_S3"
    assert states["s1"].total_gap_minutes == 165
    assert report["student_gap_minutes_change"] == 150
    assert report["students_regrouped"] == 1


# ── the guard ────────────────────────────────────────────────────────────


def test_the_guard_undoes_a_pass_that_breaks_capacity(monkeypatch):
    monkeypatch.setattr(ssa, "_seat_rule_allows", lambda _source, _dest: True)
    profiles = {f"a{i:02d}": _student(f"a{i:02d}", "AI2", ["AI236"]) for i in range(30)}
    sections = _two_sections()
    plain, plain_by_id, _r = _seat(_unmarked(sections), profiles)
    states, by_id, report = _seat(sections, profiles)
    assert report["guard_reverted"] is True
    assert report["moves"] == report["swaps"] == report["students_regrouped"] == 0
    assert _states_sig(states) == _states_sig(plain)
    assert _sections_sig(by_id.values()) == _sections_sig(plain_by_id.values())
