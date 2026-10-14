from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.services import timetable_cpsat_polisher as polisher
from core.services import timetable_local_search_chains as chains
from core.services import timetable_local_search_v2 as local_search
from core.services.timetable_assignment_models import (
    CanonicalPattern,
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_autoplace import WEEKDAYS
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate


def _pattern(name: str, start: int, family: str = "normal") -> CanonicalPattern:
    return CanonicalPattern(
        pattern_id=name,
        signature=name,
        meetings=[SectionMeeting(1, start, start + 75)],
        pattern_family=family,
        duration_permutation="75",
        is_lab_mixed=False,
        meeting_count=1,
        days_used=frozenset({1}),
        slot_fingerprint=name,
    )


def _section(code: str, pattern: CanonicalPattern) -> SectionState:
    return SectionState(
        section_id=code,
        course_code=code,
        meetings=list(pattern.meetings),
        max_capacity=30,
        reserve_capacity=0,
        pattern_id=pattern.pattern_id,
        pattern_family=pattern.pattern_family,
    )


def _profile(sid: str, courses: list[str]) -> StudentProfile:
    return StudentProfile(sid, "AI", courses, RiskTier.C, 0)


def _evaluate(sections, profiles):
    return evaluate_generated_timetable_candidate("baseline", list(sections.values()), profiles, {})


def test_stalled_unresolved_hotspot_falls_back_to_gap_improvement() -> None:
    early, middle, late = [
        _pattern(name, start) for name, start in [("early", 540), ("middle", 630), ("late", 720)]
    ]
    constrained = _pattern("only", 540, "constrained")
    sections = {
        "A": _section("A", early),
        "B": _section("B", late),
        "C": _section("C", constrained),
        "D": _section("D", constrained),
    }
    profiles = {"ab": _profile("ab", ["A", "B"]), "cd": _profile("cd", ["C", "D"])}
    baseline = _evaluate(sections, profiles)
    assert baseline.hotspot_courses == ["D"]
    assert baseline.assignment_states["ab"].total_gap_minutes == 105

    result = local_search.diagnostic_driven_local_search(
        baseline,
        sections,
        {"normal": [early, middle, late], "constrained": [constrained]},
        profiles,
        {},
        locked_section_ids={"A"},
        max_iterations=1,
    )

    assert result.lexicographic_score < baseline.lexicographic_score
    assert result.assignment_states["ab"].total_gap_minutes == 15
    assert result.hotspot_courses == ["D"]
    assert sections["B"].meetings[0].start_min == 630
    assert sections["A"].pattern_id == "early"


def test_broad_fallback_does_not_reevaluate_focused_moves(monkeypatch) -> None:
    early, late = _pattern("early", 540), _pattern("late", 720)
    sections = {"A": _section("A", early), "B": _section("B", early)}
    baseline = _evaluate(sections, {})
    baseline.hotspot_courses = ["A"]
    evaluated = []

    def unchanged(**kwargs):
        evaluated.append(tuple(s.pattern_id for s in kwargs["generated_sections"]))
        return baseline

    monkeypatch.setattr(local_search, "evaluate_generated_timetable_candidate", unchanged)
    result = local_search.diagnostic_driven_local_search(
        baseline,
        sections,
        {"normal": [early, late]},
        {},
        {},
        max_iterations=10,
    )

    assert evaluated == [("late", "early"), ("early", "late")]
    assert result is baseline
    assert all(s.pattern_id == "early" for s in sections.values())


@pytest.mark.parametrize("hotspot", ["A", "B"])
def test_swap_generation_does_not_depend_on_hotspot_section_order(hotspot) -> None:
    early, late = _pattern("early", 540), _pattern("late", 720)
    sections = {"A": _section("A", early), "B": _section("B", late)}
    moves = local_search.generate_swap_moves(sections, {"normal": [early, late]}, [hotspot])
    assert len(moves) == 1
    assert {moves[0].section_id_a, moves[0].section_id_b} == {"A", "B"}
    assert (
        local_search.generate_swap_moves(
            sections, {"normal": [early, late]}, [hotspot], locked_section_ids={hotspot}
        )
        == []
    )


def test_local_search_prioritises_instructor_idle_over_student_gap(settings) -> None:
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    early, middle, late = _pattern("early", 540), _pattern("middle", 630), _pattern("late", 720)
    sections = {
        "A": _section("A", early),
        "B": _section("B", late),
        "C": _section("C", _pattern("student_late", 810)),
    }
    profiles = {"bc": _profile("bc", ["B", "C"])}
    instructors = {"A": frozenset({7}), "B": frozenset({7}), "C": frozenset({9})}
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), profiles, {}, section_instructor_ids=instructors
    )
    assert baseline.assignment_states["bc"].total_gap_minutes == 15
    assert baseline.quality_score["instructor_schedule"]["idle_minutes"] == 105

    result = local_search.diagnostic_driven_local_search(
        baseline,
        sections,
        {"normal": [early, middle, late]},
        profiles,
        {},
        locked_section_ids={"A", "C"},
        section_instructor_ids=instructors,
        max_iterations=1,
    )

    assert result.quality_score["instructor_schedule"]["idle_minutes"] == 15
    assert result.assignment_states["bc"].total_gap_minutes == 105
    assert result.lexicographic_score > baseline.lexicographic_score
    assert sections["B"].pattern_id == "middle"


def test_zero_time_budget_returns_original_without_scanning(settings, monkeypatch) -> None:
    settings.TIMETABLE_LOCAL_SEARCH_TIME_LIMIT_SECONDS = 0
    early, late = _pattern("early", 540), _pattern("late", 720)
    sections = {"A": _section("A", early)}
    baseline = _evaluate(sections, {})

    def unexpected(**kwargs):
        pytest.fail("zero time budget must not evaluate a trial")

    monkeypatch.setattr(local_search, "evaluate_generated_timetable_candidate", unexpected)
    result = local_search.diagnostic_driven_local_search(
        baseline, sections, {"normal": [early, late]}, {}, {}
    )
    assert result is baseline
    assert sections["A"].pattern_id == "early"


@pytest.mark.parametrize("improves", [False, True])
def test_scan_deadline_rolls_back_trials_and_retains_best(monkeypatch, improves) -> None:
    import time

    early, middle, late = _pattern("early", 540), _pattern("middle", 630), _pattern("late", 720)
    sections = {"A": _section("A", early), "B": _section("B", late)}
    profiles = {"ab": _profile("ab", ["A", "B"])}
    baseline = _evaluate(sections, profiles)
    clock = [0.0]
    evaluated = []
    real_evaluate = evaluate_generated_timetable_candidate

    def timed_evaluate(**kwargs):
        evaluated.append(kwargs["generated_sections"][1].pattern_id)
        result = real_evaluate(**kwargs) if improves else baseline
        clock[0] = 181.0
        return result

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(local_search, "evaluate_generated_timetable_candidate", timed_evaluate)
    # The first B move is middle (improves); early would clash with locked A.
    result = local_search.diagnostic_driven_local_search(
        baseline,
        sections,
        {"normal": [middle, early, late]},
        profiles,
        {},
        locked_section_ids={"A"},
        max_iterations=50,
    )

    assert evaluated == ["middle"]
    assert sections["A"].pattern_id == "early"
    assert sections["B"].pattern_id == ("middle" if improves else "late")
    assert result.assignment_states["ab"].total_gap_minutes == (15 if improves else 105)


def _fake_scenario(monkeypatch, lecture_slots, lab_slots=None, day="MON"):
    starts = {s["start"] for s in lecture_slots + (lab_slots or [])}
    scenario = SimpleNamespace(
        slot_config=lecture_slots,
        lab_slot_config=lab_slots,
        blocked_slots=[
            {"day": d, "start": start} for d in WEEKDAYS if d != day for start in starts
        ],
    )
    monkeypatch.setattr(
        polisher.DeliveryBoard.objects,
        "filter",
        lambda **kwargs: SimpleNamespace(first=lambda: SimpleNamespace(scenario=scenario)),
    )
    monkeypatch.setattr(polisher, "build_cross_board_overlap_matrix", lambda sid: {("A", "B"): 1})


@pytest.mark.parametrize("day", ["SUN", "MON", "TUE", "WED", "THU"])
@pytest.mark.parametrize("fixed_interval", [(540, 615), (570, 630)])
def test_fixed_cross_course_overlap_penalty_works_every_day(
    monkeypatch, day, fixed_interval
) -> None:
    _fake_scenario(
        monkeypatch,
        [
            {"start": "09:00", "end": "10:15"},
            {"start": "10:30", "end": "11:45"},
        ],
        day=day,
    )
    sections = {code: _section(code, _pattern("early", 540)) for code in ["A", "B"]}
    for section in sections.values():
        section.meetings = [SectionMeeting(WEEKDAYS.index(day), 540, 615)]
    sections["B"].meetings = [SectionMeeting(WEEKDAYS.index(day), *fixed_interval)]
    profiles = {"ab": _profile("ab", ["A", "B"])}
    baseline = _evaluate(sections, profiles)

    result = polisher.polish_scenario_with_cpsat(
        123,
        list(sections.values()),
        profiles,
        {},
        baseline,
        locked_section_ids={"B"},
        time_limit_seconds=2,
    )

    assert result is not None
    assert result["eval"].unresolved_student_ids == []
    assert result["improved_sections"][0].meetings == [
        SectionMeeting(WEEKDAYS.index(day), 630, 705)
    ]
    assert sections["A"].meetings[0].start_min == 540
    assert sections["B"].meetings == [SectionMeeting(WEEKDAYS.index(day), *fixed_interval)]


@pytest.mark.parametrize("duration", [50, 75, 100, 150])
def test_polisher_preserves_duration_with_mixed_custom_grids(monkeypatch, duration) -> None:
    lecture_slots = [
        {"start": "09:00", "end": "09:50"},
        {"start": "10:00", "end": "11:15"},
        {"start": "12:00", "end": "12:50"},
        {"start": "13:00", "end": "14:15"},
    ]
    lab_slots = [
        {"start": "09:00", "end": "10:40"},
        {"start": "11:00", "end": "13:30"},
        {"start": "14:00", "end": "15:40"},
        {"start": "16:00", "end": "18:30"},
    ]
    _fake_scenario(monkeypatch, lecture_slots, lab_slots)
    sections = {code: _section(code, _pattern("early", 540)) for code in ["A", "B"]}
    for section in sections.values():
        section.meetings = [SectionMeeting(1, 540, 540 + duration)]
    profiles = {"ab": _profile("ab", ["A", "B"])}
    baseline = _evaluate(sections, profiles)

    result = polisher.polish_scenario_with_cpsat(
        123,
        list(sections.values()),
        profiles,
        {},
        baseline,
        locked_section_ids={"B"},
        time_limit_seconds=2,
    )

    assert result is not None
    assert result["eval"].unresolved_student_ids == []
    assert all(
        m.end_min - m.start_min == duration for s in result["improved_sections"] for m in s.meetings
    )


@pytest.mark.parametrize(
    "slots",
    [
        [{"start": "09:00", "end": "09:50"}],
        [{"start": "09:00", "end": "10:15", "online_only": True}],
    ],
)
def test_polisher_keeps_original_when_no_matching_placeable_slot(monkeypatch, slots) -> None:
    _fake_scenario(monkeypatch, slots)
    sections = {"A": _section("A", _pattern("early", 540))}
    baseline = _evaluate(sections, {})

    assert (
        polisher.polish_scenario_with_cpsat(123, list(sections.values()), {}, {}, baseline) is None
    )
    assert sections["A"].meetings == [SectionMeeting(1, 540, 615)]


def _instructor_chain_board():
    a_old, a_new = _pattern("a_old", 540, "a"), _pattern("a_new", 690, "a")
    b_old, b_new = _pattern("b_old", 720, "b"), _pattern("b_new", 600, "b")
    sections = {"A": _section("A", a_old), "B": _section("B", b_old)}
    catalog = {"a": [a_old, a_new], "b": [b_old, b_new]}
    instructors = {"A": frozenset({7}), "B": frozenset({7})}
    return sections, catalog, instructors


def test_instructor_only_chain_escapes_single_move_clash_trap(settings) -> None:
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    sections, catalog, instructors = _instructor_chain_board()
    # The students share no courses, so only the instructor links A and B.
    profiles = {"a": _profile("a", ["A"]), "b": _profile("b", ["B"])}
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), profiles, {}, section_instructor_ids=instructors
    )
    assert baseline.hotspot_courses == []
    assert baseline.quality_score["instructor_schedule"]["idle_minutes"] == 105
    single = local_search.diagnostic_driven_local_search(
        baseline, sections, catalog, profiles, {}, section_instructor_ids=instructors
    )
    assert single is baseline  # Either move alone double-books the teacher.

    result = chains.chain_local_search(
        baseline,
        sections,
        catalog,
        profiles,
        {},
        section_instructor_ids=instructors,
        max_iterations=1,
    )

    assert result.quality_score["instructor_schedule"]["idle_minutes"] == 15
    assert result.quality_score["instructor_schedule"]["working_days"] == 1
    assert result.lexicographic_score == baseline.lexicographic_score
    assert sections["A"].pattern_id == "a_new"
    assert sections["B"].pattern_id == "b_new"


def test_student_linked_focused_pair_is_generated_once_with_legacy_policy(settings) -> None:
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    sections, catalog, instructors = _instructor_chain_board()
    assert (
        chains.generate_chain_2_moves([], sections, catalog, {}, section_instructor_ids=instructors)
        == []
    )
    profiles = {"ab": _profile("ab", ["A", "B"])}
    generated = chains.generate_chain_2_moves(["A", "B"], sections, catalog, profiles)
    assert len(generated) == 1
    assert {generated[0].move_a.section_id_a, generated[0].move_b.section_id_a} == {"A", "B"}
    baseline = _evaluate(sections, profiles)
    baseline.hotspot_courses = ["A", "B"]
    result = chains.chain_local_search(baseline, sections, catalog, profiles, {}, max_iterations=1)
    assert result.assignment_states["ab"].total_gap_minutes == 15
    assert result.lexicographic_score < baseline.lexicographic_score


def test_teacher_focus_does_not_displace_original_student_hotspot_targets(settings) -> None:
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    sections, catalog, instructors = _instructor_chain_board()
    for index in range(5):
        code = f"Teacher{index}"
        old, new = _pattern(f"{code}_old", 540, code), _pattern(f"{code}_new", 630, code)
        sections[code] = _section(code, old)
        sections[code].meetings = [SectionMeeting(day, 540, 615) for day in range(5)]
        catalog[code] = [old, new]
        instructors[code] = frozenset({index + 20})
    assert all(
        code.startswith("Teacher")
        for code in chains.instructor_focus_courses(sections.values(), instructors, limit=5)
    )
    profiles = {"ab": _profile("ab", ["A", "B"])}
    generated = chains.generate_chain_2_moves(
        ["A"], sections, catalog, profiles, section_instructor_ids=instructors
    )
    assert any({c.move_a.section_id_a, c.move_b.section_id_a} == {"A", "B"} for c in generated)


@pytest.mark.parametrize("frozen", ["locked", "online", "duration"])
def test_instructor_chain_preserves_locks_modality_and_contact_time(settings, frozen) -> None:
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    sections, catalog, instructors = _instructor_chain_board()
    locked = set()
    if frozen == "locked":
        locked.add("B")
    elif frozen == "online":
        sections["B"].is_online = True
    else:
        mismatched = catalog["b"][1]
        catalog["b"] = [
            catalog["b"][0],
            CanonicalPattern(
                pattern_id=mismatched.pattern_id,
                signature=mismatched.signature,
                meetings=[SectionMeeting(1, 600, 650)],
                pattern_family="b",
                duration_permutation="50",
                is_lab_mixed=False,
                meeting_count=1,
                days_used=frozenset({1}),
                slot_fingerprint="bad-duration",
            ),
        ]
    assert (
        chains.generate_chain_2_moves(
            [], sections, catalog, {}, locked_section_ids=locked, section_instructor_ids=instructors
        )
        == []
    )
    assert sections["A"].pattern_id == "a_old"
    assert sections["B"].pattern_id == "b_old"


def test_teacher_only_chain_requires_actual_section_instructor_link(settings) -> None:
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    sections, catalog, instructors = _instructor_chain_board()
    sections["A_other"] = _section("A", catalog["a"][0])
    sections["A_other"].section_id = "A_other"
    instructors["A_other"] = frozenset({9})
    generated = chains.generate_chain_2_moves(
        [], sections, catalog, {}, section_instructor_ids=instructors
    )
    assert len(generated) == 1
    assert {generated[0].move_a.section_id_a, generated[0].move_b.section_id_a} == {"A", "B"}


def test_instructor_chain_deadline_banks_winner_after_rolling_back_trial(
    settings, monkeypatch
) -> None:
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    sections, catalog, instructors = _instructor_chain_board()
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), {}, {}, section_instructor_ids=instructors
    )
    clock = [0.0]
    evaluations = []

    def timed_evaluate(**kwargs):
        result = evaluate_generated_timetable_candidate(**kwargs)
        evaluations.append(result)
        clock[0] = 2.0
        return result

    monkeypatch.setattr(chains.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(chains, "evaluate_generated_timetable_candidate", timed_evaluate)
    result = chains.chain_local_search(
        baseline,
        sections,
        catalog,
        {},
        {},
        section_instructor_ids=instructors,
        chain_time_limit_seconds=1.0,
    )
    assert len(evaluations) == 1
    assert result.quality_score["instructor_schedule"]["idle_minutes"] == 15
    assert sections["A"].pattern_id == "a_new"
    assert sections["B"].pattern_id == "b_new"


def _custom_pattern(name: str, start: int, durations: tuple[int, ...], family="normal"):
    return replace(
        _pattern(name, start, family),
        meetings=[
            SectionMeeting(day, start, start + duration) for day, duration in enumerate(durations)
        ],
        duration_permutation="+".join(map(str, durations)),
        meeting_count=len(durations),
        days_used=frozenset(range(len(durations))),
    )


@pytest.mark.parametrize("duration", [50, 60, 100])
def test_repattern_preserves_imported_contact_minutes_and_actual_family(duration) -> None:
    original = _custom_pattern("original", 540, (duration,))
    valid = _custom_pattern("valid", 630, (duration,))
    wrong_duration = _custom_pattern("wrong-duration", 630, (75,))
    wrong_family = _custom_pattern("wrong-family", 630, (duration,), family="other")
    sections = {"A": _section("A", original)}
    catalog = {"normal": [original, wrong_duration, wrong_family, valid]}
    generated = local_search.generate_all_repattern_moves(sections, catalog)
    assert [move.to_pattern_id_a for move in generated] == ["valid"]
    snapshot = local_search.apply_move_to_grid(generated[0], sections, catalog)
    assert [m.end_min - m.start_min for m in sections["A"].meetings] == [duration]
    local_search._rollback(snapshot, sections, None)
    assert sections["A"].meetings == original.meetings


@pytest.mark.parametrize("duration", [50, 60, 100])
@pytest.mark.parametrize("changed_leg", ["A", "B"])
def test_swap_checks_contact_minutes_for_each_destination(duration, changed_leg) -> None:
    early, late = _pattern("early", 540), _pattern("late", 720)
    sections = {"A": _section("A", early), "B": _section("B", late)}
    section = sections[changed_leg]
    meeting = section.meetings[0]
    section.meetings = [
        SectionMeeting(meeting.day, meeting.start_min, meeting.start_min + duration)
    ]
    assert local_search.generate_swap_moves(sections, {"normal": [early, late]}, ["A", "B"]) == []


@pytest.mark.parametrize("duration", [50, 60, 100])
def test_swap_accepts_matching_custom_duration_destinations(duration) -> None:
    early = _custom_pattern("early", 540, (duration,))
    late = _custom_pattern("late", 720, (duration,))
    sections = {"A": _section("A", early), "B": _section("B", late)}
    catalog = {"normal": [early, late]}
    generated = local_search.generate_swap_moves(sections, catalog, ["B"])
    assert len(generated) == 1
    local_search.apply_move_to_grid(generated[0], sections, catalog)
    assert sections["A"].meetings == late.meetings
    assert sections["B"].meetings == early.meetings
    assert all(m.end_min - m.start_min == duration for s in sections.values() for m in s.meetings)


@pytest.mark.parametrize("bad_destination", ["missing", "family"])
@pytest.mark.parametrize("changed_leg", ["A", "B"])
def test_swap_rejects_missing_or_wrong_family_destination(bad_destination, changed_leg) -> None:
    early, late, other = _pattern("early", 540), _pattern("late", 720), _pattern("other", 810)
    sections = {"A": _section("A", early), "B": _section("B", late)}
    patterns = [early, late, other]
    target_id = sections[changed_leg].pattern_id
    if bad_destination == "missing":
        patterns = [pattern for pattern in patterns if pattern.pattern_id != target_id]
    else:
        patterns = [
            replace(pattern, pattern_family="other-family")
            if pattern.pattern_id == target_id
            else pattern
            for pattern in patterns
        ]
    assert local_search.generate_swap_moves(sections, {"normal": patterns}, ["A", "B"]) == []


def test_repattern_preserves_duration_multiset_not_only_weekly_total() -> None:
    original = _custom_pattern("original", 540, (50, 100))
    equal_total = _custom_pattern("wrong-multiset", 630, (75, 75))
    valid_permutation = _custom_pattern("valid-permutation", 630, (100, 50))
    sections = {"A": _section("A", original)}
    generated = local_search.generate_all_repattern_moves(
        sections, {"normal": [original, equal_total, valid_permutation]}
    )
    assert [move.to_pattern_id_a for move in generated] == ["valid-permutation"]


def test_chain_generation_obeys_expired_deadline_without_mutating_board(
    settings, monkeypatch
) -> None:
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    sections, catalog, instructors = _instructor_chain_board()
    monkeypatch.setattr(chains.time, "perf_counter", lambda: 2.0)
    assert (
        chains.generate_chain_2_moves(
            [], sections, catalog, {}, section_instructor_ids=instructors, deadline=1.0
        )
        == []
    )
    assert sections["A"].pattern_id == "a_old"
    assert sections["B"].pattern_id == "b_old"
