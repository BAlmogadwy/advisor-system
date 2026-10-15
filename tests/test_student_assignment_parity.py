"""Exact-equivalence guard for the optimised student-assignment evaluator.

``tests/_reference_student_assignment.py`` is a frozen copy of the evaluator as
it was BEFORE the performance work. Every case below runs the same board through
the frozen reference and through production and demands bit-identical output:
the per-student assignments (including dict/set insertion order), unresolved
reasons, gap totals, section end-states, the lexicographic score tuple and the
quality dict.

Inputs are (a) an anonymised real board (scenarios 663 / 666, with random
perturbations: capacities, reserves, pre-filled seats, moved / swapped /
overlapping meetings, online flags, tied risk scores) and (b) small synthetic
boards built to be tie-heavy (identical patterns, identical capacities, tied
rigidity) so tie-breaking order is exercised, not just the happy path.
"""

from __future__ import annotations

import json
import random
from copy import deepcopy
from pathlib import Path

import pytest
from django.test import override_settings

from core.services import timetable_student_assignment as prod
from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
    StudentProfile,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_quality import student_day_overload_penalty
from tests import _reference_student_assignment as ref

FIXTURE = Path(__file__).parent / "fixtures" / "student_assignment_parity.json"

FLAG_COMBOS = [
    {"TIMETABLE_TIERED_OBJECTIVE_ENABLED": True, "TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED": False},
    {
        "TIMETABLE_TIERED_OBJECTIVE_ENABLED": False,
        "TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED": False,
    },
    {"TIMETABLE_TIERED_OBJECTIVE_ENABLED": True, "TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED": True},
    {"TIMETABLE_TIERED_OBJECTIVE_ENABLED": False, "TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED": True},
]


# ── case construction ────────────────────────────────────────────────────


class Case:
    def __init__(self, sections, profiles, rigidity, tiers, instructors):
        self.sections = sections
        self.profiles = profiles
        self.rigidity = rigidity
        self.tiers = tiers
        self.instructors = instructors


def _section(sid, course, meetings, cap, reserve, enrolled=0, online=False):
    return SectionState(
        section_id=sid,
        course_code=course,
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for d, s, e in meetings],
        max_capacity=cap,
        reserve_capacity=reserve,
        current_enrollment=enrolled,
        enrolled_student_ids={f"pre{i}" for i in range(enrolled)},
        is_online=online,
    )


def _profile(sid, courses, tier, score):
    return StudentProfile(
        student_id=sid,
        department="X",
        recommended_courses=list(courses),
        risk_tier=RiskTier(tier),
        intra_tier_score=score,
    )


_FX_CACHE: dict = {}


def _fixture(key: str) -> dict:
    if not _FX_CACHE:
        _FX_CACHE.update(json.loads(FIXTURE.read_text()))
    return _FX_CACHE[key]


def _perturbed_real_case(key: str, seed: int) -> Case:
    rng = random.Random(seed)
    fx = _fixture(key)
    sections = []
    for row in fx["sections"]:
        meetings = [list(m) for m in row["meetings"]]
        cap, reserve, enrolled = row["max"], row["reserve"], row["enrolled"]
        online = row["online"]
        roll = rng.random()
        if roll < 0.30:  # capacity / reserve / pre-filled seats
            cap = max(1, cap + rng.randint(-12, 8))
            reserve = rng.randint(0, min(cap, cap // 4 + 1))
            enrolled = rng.randint(0, cap // 2)
        elif roll < 0.55:  # move: shift in time and/or change day
            shift = rng.randrange(-60, 61, 5)
            for m in meetings:
                if rng.random() < 0.5:
                    m[0] = rng.randrange(0, 5)
                length = m[2] - m[1]
                m[1] = min(max(480, m[1] + shift), 1000)
                m[2] = m[1] + length
        elif roll < 0.62:  # force identical-pattern ties
            meetings = [[1, 540, 615], [3, 540, 615]]
        if rng.random() < 0.10:
            online = not online
        sections.append(
            _section(row["id"], row["course"], meetings, cap, reserve, enrolled, online)
        )
    if rng.random() < 0.5 and len(sections) > 2:  # swap two sections' meetings
        a, b = rng.sample(range(len(sections)), 2)
        sections[a].meetings, sections[b].meetings = sections[b].meetings, sections[a].meetings
    profile_rows = list(fx["profiles"])
    if rng.random() < 0.6:
        profile_rows = rng.sample(profile_rows, rng.randint(40, len(profile_rows)))
    profiles = {}
    for row in profile_rows:
        tier = row["tier"] if rng.random() > 0.15 else rng.randint(1, 3)
        score = row["score"] if rng.random() > 0.3 else float(rng.choice([0, 1, 2]))
        courses = [c for c in row["courses"] if rng.random() > 0.05]
        profiles[row["id"]] = _profile(row["id"], courses, tier, score)
    instructors = {k: frozenset(v) for k, v in fx["instructors"].items()} or None
    return Case(sections, profiles, dict(fx["rigidity"]), dict(fx["tiers"]), instructors)


_SLOTS = [(480, 555), (480, 570), (570, 645), (630, 705), (720, 795), (780, 855), (840, 915)]


def _synthetic_case(seed: int) -> Case:
    rng = random.Random(seed)
    courses = [f"C{i}" for i in range(rng.randint(3, 12))]
    sections = []
    for c in courses:
        for k in range(rng.choice([0, 1, 1, 2, 2, 3, 4])):
            meetings = []
            for _ in range(rng.choice([1, 1, 2, 3])):
                s, e = rng.choice(_SLOTS)
                meetings.append((rng.randrange(0, 5), s, e))
            if rng.random() < 0.08 and meetings:  # a section overlapping itself
                d, s, e = meetings[0]
                meetings.append((d, s + 15, e + 15))
            cap = rng.choice([0, 1, 2, 3, 5, 40])
            reserve = rng.randint(0, cap)
            enrolled = rng.randint(0, cap) if rng.random() < 0.3 else 0
            sections.append(
                _section(f"{c}_S{k}", c, meetings, cap, reserve, enrolled, rng.random() < 0.15)
            )
    pool = courses + ["NOSEC1", "NOSEC2"]
    profiles = {}
    for i in range(rng.randint(4, 70)):
        sid = f"st{i:03d}"
        rec = rng.sample(pool, rng.randint(1, min(7, len(pool))))
        profiles[sid] = _profile(sid, rec, rng.randint(1, 3), rng.choice([0.0, 0.0, 1.0, 2.5]))
    rigidity = {c: rng.choice([0.0, 0.5, 1.0]) for c in courses if rng.random() < 0.8}
    tiers = {c: rng.choice(["T1", "T2", "T3"]) for c in courses if rng.random() < 0.9}
    instructors = None
    if rng.random() < 0.5:
        instructors = {s.section_id: frozenset({rng.randint(1, 3)}) for s in sections}
    return Case(sections, profiles, rigidity, tiers, instructors)


# ── comparison ───────────────────────────────────────────────────────────


def _state_sig(st: StudentAssignmentState):
    return (
        st.student_id,
        list(st.assigned_sections.items()),
        list(st.section_ids),
        list(st.occupied_mask_by_day.items()),
        st.total_gap_minutes,
        list(st.unresolved_courses.items()),
    )


def _states_sig(states):
    return [_state_sig(st) for st in states.values()]


def _sections_sig(sections):
    return [
        (s.section_id, s.current_enrollment, sorted(s.enrolled_student_ids), s.assigned_room_id)
        for s in sections
    ]


def _input_sig(sections):
    return [
        (
            s.section_id,
            s.course_code,
            [(m.day, m.start_min, m.end_min, m.mask) for m in s.meetings],
            s.max_capacity,
            s.reserve_capacity,
            s.current_enrollment,
            sorted(s.enrolled_student_ids),
            s.is_online,
        )
        for s in sections
    ]


def _result_sig(res):
    return (
        res.candidate_id,
        res.lexicographic_score,
        _states_sig(res.assignment_states),
        res.unresolved_student_ids,
        res.hotspot_courses,
        res.capacity_pressure_courses,
        res.reserve_heavy_sections,
        res.quality_score,
        res.instructor_overload_count,
    )


def _assert_case_identical(case: Case, flags: dict, use_tiers: bool) -> None:
    tiers = case.tiers if use_tiers else None
    with override_settings(**flags):
        # Direct level: assignment + section end-states + score.
        secs_r = deepcopy(case.sections)
        secs_p = deepcopy(case.sections)
        by_id_r = ref.build_sections_by_id(secs_r)
        by_id_p = prod.build_sections_by_id(secs_p)
        states_r, unres_r = ref.assign_students_to_sections(
            case.profiles,
            by_id_r,
            ref.build_sections_by_course(by_id_r),
            case.rigidity,
            course_tiers=tiers,
        )
        states_p, unres_p = prod.assign_students_to_sections(
            case.profiles,
            by_id_p,
            prod.build_sections_by_course(by_id_p),
            case.rigidity,
            course_tiers=tiers,
        )
        assert unres_p == unres_r
        assert _states_sig(states_p) == _states_sig(states_r)
        assert _sections_sig(secs_p) == _sections_sig(secs_r)
        score_r = ref.evaluate_assignability_lexicographic(
            states_r, case.profiles, by_id_r, case.instructors, tiers
        )
        score_p = prod.evaluate_assignability_lexicographic(
            states_p, case.profiles, by_id_p, case.instructors, tiers
        )
        assert score_p == score_r
        assert student_day_overload_penalty(
            states_p, by_id_p
        ) == ref.reference_student_day_overload_penalty(states_r, by_id_r)

        # Integration level: the whole candidate evaluation, and it must not
        # touch the caller's sections.
        before = _input_sig(case.sections)
        kwargs = dict(
            candidate_id="c",
            generated_sections=case.sections,
            student_profiles=case.profiles,
            course_rigidity=case.rigidity,
            section_instructor_ids=case.instructors,
            course_tiers=tiers,
        )
        got = evaluate_generated_timetable_candidate(**kwargs)
        assert _input_sig(case.sections) == before
        want = ref.reference_evaluate_candidate(**kwargs)
        assert _input_sig(case.sections) == before
        assert _result_sig(got) == _result_sig(want)


# ── tests ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", range(300))
def test_synthetic_boards_match_frozen_reference(seed: int) -> None:
    case = _synthetic_case(seed)
    flags = FLAG_COMBOS[seed % len(FLAG_COMBOS)]
    _assert_case_identical(case, flags, use_tiers=bool(seed % 5))


@pytest.mark.parametrize("key", ["663", "666"])
@pytest.mark.parametrize("seed", range(40))
def test_perturbed_real_boards_match_frozen_reference(key: str, seed: int) -> None:
    case = _perturbed_real_case(key, seed)
    flags = FLAG_COMBOS[seed % len(FLAG_COMBOS)]
    _assert_case_identical(case, flags, use_tiers=bool(seed % 6))


@pytest.mark.parametrize("key", ["663", "666"])
def test_unperturbed_real_boards_match_frozen_reference(key: str) -> None:
    fx = _fixture(key)
    case = Case(
        [
            _section(
                r["id"],
                r["course"],
                r["meetings"],
                r["max"],
                r["reserve"],
                r["enrolled"],
                r["online"],
            )
            for r in fx["sections"]
        ],
        {r["id"]: _profile(r["id"], r["courses"], r["tier"], r["score"]) for r in fx["profiles"]},
        dict(fx["rigidity"]),
        dict(fx["tiers"]),
        {k: frozenset(v) for k, v in fx["instructors"].items()} or None,
    )
    for flags in FLAG_COMBOS:
        _assert_case_identical(case, flags, use_tiers=True)


# ── public helpers on hand-built mid-flight states ───────────────────────


def _walk_public_api(seed: int) -> None:
    """Drive apply / remove / rank / gap / assign through both modules in lock-step."""
    case = _synthetic_case(10_000 + seed)
    rng = random.Random(seed)
    secs_r = deepcopy(case.sections)
    secs_p = deepcopy(case.sections)
    by_id_r = {s.section_id: s for s in secs_r}
    by_id_p = {s.section_id: s for s in secs_p}
    by_course_r = ref.build_sections_by_course(by_id_r)
    by_course_p = prod.build_sections_by_course(by_id_p)
    st_r = StudentAssignmentState(student_id="X")
    st_p = StudentAssignmentState(student_id="X")
    ids = [s.section_id for s in secs_r]
    if not ids:
        return
    for _ in range(40):
        action = rng.choice(["apply", "apply", "remove", "rank", "gap", "assign"])
        sid = rng.choice(ids)
        sr, sp = by_id_r[sid], by_id_p[sid]
        if action == "apply":
            # Only seat what the real engine would: no clash, course not yet held.
            if sr.course_code in st_r.assigned_sections or st_r.has_clash(sr.meetings):
                continue
            ref.apply_assignment(st_r, sr, by_id_r)
            prod.apply_assignment(st_p, sp, by_id_p)
        elif action == "remove":
            ref.remove_assignment(st_r, sr, by_id_r)
            prod.remove_assignment(st_p, sp, by_id_p)
        elif action == "gap":
            assert prod.calculate_added_gap(st_p, sp, by_id_p) == ref.calculate_added_gap(
                st_r, sr, by_id_r
            )
        elif action == "rank":
            allow = rng.random() < 0.5
            cands_r = by_course_r.get(sr.course_code, [])
            cands_p = by_course_p.get(sp.course_code, [])
            got = prod.rank_and_select_best_section(cands_p, st_p, allow, by_id_p)
            want = ref.rank_and_select_best_section(cands_r, st_r, allow, by_id_r)
            assert (got is None) == (want is None)
            if got:
                assert got[0].section_id == want[0].section_id and got[1] == want[1]
        else:
            profile = rng.choice(list(case.profiles.values()))
            released = rng.random() < 0.5
            tiers = case.tiers if rng.random() < 0.5 else None
            # Both a fresh student and the partly-seated walking one (whose
            # existing seats make the scarcity count and the picks clash-aware).
            fresh_r, fresh_p = StudentAssignmentState("Y"), StudentAssignmentState("Y")
            for sr_, sp_ in ((fresh_r, fresh_p), (st_r, st_p)):
                ref.assign_courses_for_student(
                    profile, sr_, by_course_r, by_id_r, case.rigidity, released, course_tiers=tiers
                )
                prod.assign_courses_for_student(
                    profile, sp_, by_course_p, by_id_p, case.rigidity, released, course_tiers=tiers
                )
                assert _state_sig(sp_) == _state_sig(sr_)
        assert _state_sig(st_p) == _state_sig(st_r)
        assert _sections_sig(secs_p) == _sections_sig(secs_r)
    # The repair pass on a partly-seated board, driven as a public entry point.
    states_r = {"X": st_r}
    states_p = {"X": st_p}
    profiles = {"X": _profile("X", list(by_course_r), 2, 0.0)}
    for states, by_id, by_course, mod in (
        (states_r, by_id_r, by_course_r, ref),
        (states_p, by_id_p, by_course_p, prod),
    ):
        states["X"].unresolved_courses.update(
            {
                c: mod.UnresolvedReason(c, "mixed_blockers")
                for c in by_course
                if c not in states["X"].assigned_sections
            }
        )
        mod.repair_unresolved_assignments_shallow(states, profiles, by_course, by_id)
    assert _states_sig(states_p) == _states_sig(states_r)
    assert _sections_sig(secs_p) == _sections_sig(secs_r)


@pytest.mark.parametrize("seed", range(120))
def test_public_helpers_match_frozen_reference(seed: int) -> None:
    _walk_public_api(seed)


def test_fill_ratio_truncates_like_the_reference() -> None:
    """33.33% and 33.3378% both truncate to 3333bp, so the section id decides.

    A ``round()`` instead of ``int()`` would split them (3333 vs 3334) and flip
    the winner -- the one place the ranking depends on float truncation.
    """
    sections = {
        "B_late": _section("B_late", "C", [(0, 480, 555)], cap=3, reserve=0, enrolled=0),
        "A_early": _section("A_early", "C", [(0, 480, 555)], cap=7499, reserve=0, enrolled=2499),
    }
    for mod in (ref, prod):
        picked = mod.rank_and_select_best_section(
            list(sections.values()), StudentAssignmentState("X"), True, sections
        )
        assert picked is not None
        assert picked[0].section_id == "A_early"
        assert picked[1] == (0, 0, 0, 3333, "A_early")
