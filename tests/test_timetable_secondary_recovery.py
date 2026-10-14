from copy import deepcopy

import pytest
from django.test import override_settings

from core.services.timetable_assignment_models import (
    CanonicalPattern,
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_exact_rooming import ExactRoomingError
from core.services.timetable_secondary_recovery import (
    _recoverable_room_targets,
    recover_secondary_quality,
)


def pattern(start, *, day=0, duration=60):
    meeting = SectionMeeting(day, start, start + duration)
    return CanonicalPattern(
        f"p{day}_{start}_{duration}",
        "",
        [meeting],
        "lecture60",
        str(duration),
        False,
        1,
        frozenset({day}),
        "",
    )


def section(sid, course, start):
    pat = pattern(start)
    return SectionState(
        sid,
        course,
        list(pat.meetings),
        30,
        0,
        pattern_id=pat.pattern_id,
        pattern_family=pat.pattern_family,
    )


def run(sections, patterns, rooms, *, teachers=None, locked=None, **kwargs):
    profiles = {"s": StudentProfile("s", "CS", ["CORE", "SOFT"], RiskTier.C, 0)}
    tiers = {"CORE": "T1", "SOFT": "T3"}
    baseline = evaluate_generated_timetable_candidate(
        "before", list(sections.values()), profiles, {}, teachers, tiers
    )
    return recover_secondary_quality(
        current_eval=baseline,
        sections_by_id=sections,
        pattern_catalog={"lecture60": patterns},
        student_profiles=profiles,
        course_rigidity={},
        room_evaluator=rooms,
        section_instructor_ids=teachers,
        course_tiers=tiers,
        locked_section_ids=locked or {"core"},
        time_limit_seconds=10,
        max_iterations=3,
        **kwargs,
    )


def room_result(count):
    return {
        "unassigned_count": count,
        "unassigned_section_ids": ["soft"] if count else [],
        "proven_optimal": True,
    }


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_recovers_soft_seat_and_room_without_changing_teacher_or_input():
    sections = {"core": section("core", "CORE", 540), "soft": section("soft", "SOFT", 540)}
    original = deepcopy(sections)

    def rooms(states):
        by_id = {s.section_id: s for s in states}
        return room_result(int(by_id["soft"].meetings == by_id["core"].meetings))

    result = run(sections, [pattern(540), pattern(600)], rooms, teachers={"core": frozenset({7})})
    report = result["report"]
    assert report["applied"]
    assert (report["soft_unresolved_before"], report["soft_unresolved_after"]) == (1, 0)
    assert (report["room_shortages_before"], report["room_shortages_after"]) == (1, 0)
    assert report["instructor_before"] == report["instructor_after"]
    assert sections == original


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_soft_seat_cannot_be_paid_for_with_more_room_shortages():
    sections = {"core": section("core", "CORE", 540), "soft": section("soft", "SOFT", 540)}

    def rooms(states):
        soft = next(s for s in states if s.section_id == "soft")
        return room_result(1 if soft.meetings[0].start_min == 540 else 2)

    result = run(sections, [pattern(540), pattern(600)], rooms)
    assert not result["report"]["applied"]
    assert result["report"]["room_shortages_after"] == 1


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_room_recovery_cannot_drop_a_soft_seat():
    sections = {"core": section("core", "CORE", 540), "soft": section("soft", "SOFT", 600)}

    def rooms(states):
        soft = next(s for s in states if s.section_id == "soft")
        return room_result(int(soft.meetings[0].start_min == 600))

    result = run(sections, [pattern(540), pattern(600)], rooms)
    assert not result["report"]["applied"]
    assert result["report"]["soft_unresolved_after"] == 0


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_secondary_gain_cannot_add_teacher_idle():
    sections = {
        "core": section("core", "CORE", 540),
        "soft": section("soft", "SOFT", 540),
        "anchor": section("anchor", "OTHER", 600),
    }
    result = run(
        sections,
        [pattern(540), pattern(720)],
        lambda _states: room_result(0),
        teachers={"soft": frozenset({7}), "anchor": frozenset({7})},
        locked={"core", "anchor"},
    )
    assert not result["report"]["applied"]
    assert result["report"]["instructor_rejections"] > 0


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_remaining_budget_polishes_student_gaps_with_counts_and_teachers_frozen():
    sections = {"core": section("core", "CORE", 540), "soft": section("soft", "SOFT", 780)}
    result = run(
        sections,
        [pattern(540), pattern(600), pattern(780)],
        lambda _states: room_result(0),
        teachers={"core": frozenset({7})},
    )
    report = result["report"]
    assert report["soft_unresolved_before"] == report["soft_unresolved_after"] == 0
    assert report["room_shortages_before"] == report["room_shortages_after"] == 0
    assert report["instructor_before"] == report["instructor_after"]
    assert report["student_gap_minutes_after"] < report["student_gap_minutes_before"]
    assert report["score_after"] < report["score_before"]
    assert report["accepted_moves"][0]["phase"] == "polish"


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_polishing_after_recovery_retains_newly_recovered_seat():
    sections = {"core": section("core", "CORE", 540), "soft": section("soft", "SOFT", 540)}
    result = run(
        sections,
        [pattern(540), pattern(780), pattern(600)],
        lambda _states: room_result(0),
    )
    report = result["report"]
    assert report["soft_unresolved_after"] == 0
    assert report["student_gap_minutes_after"] == 0
    assert [move["phase"] for move in report["accepted_moves"]] == ["recovery", "polish"]


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_room_oracle_rejects_invalid_trial_without_losing_the_board():
    sections = {"core": section("core", "CORE", 540), "soft": section("soft", "SOFT", 540)}

    def rooms(states):
        soft = next(s for s in states if s.section_id == "soft")
        if soft.meetings[0].start_min != 540:
            raise ExactRoomingError("Frozen physical meeting differs")
        return room_result(1)

    result = run(sections, [pattern(540), pattern(600)], rooms)
    assert not result["report"]["applied"]
    assert result["report"]["structural_rejections"] > 0
    assert result["improved_sections"] == list(sections.values())


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_same_soft_trial_outside_room_congestion_skips_expensive_room_oracle():
    sections = {
        "core": section("core", "CORE", 540),
        "soft": section("soft", "SOFT", 540),
        "room_target": section("room_target", "LAB", 800),
    }
    calls = 0

    def rooms(_states):
        nonlocal calls
        calls += 1
        return {
            **room_result(1),
            "failures": [{"section_id": "room_target", "reason": "ROOM_OCCUPIED"}],
        }

    result = run(
        sections,
        [pattern(540), pattern(570)],
        rooms,
        locked={"core", "room_target"},
        max_polish_iterations=0,
    )
    assert calls == 1
    assert result["report"]["unrelated_room_trials_skipped"] > 0
    assert not result["report"]["applied"]


def test_room_targets_include_overlapping_blockers_but_not_impossible_inventory():
    sections = {
        "stranded": section("stranded", "A", 540),
        "blocker": section("blocker", "B", 570),
        "impossible": section("impossible", "C", 800),
    }
    result = {
        "failures": [
            {"section_id": "stranded", "reason": "ROOM_OCCUPIED"},
            {"section_id": "impossible", "reason": "NO_ROOM_CAPACITY"},
        ]
    }
    assert _recoverable_room_targets(result, sections) == {"stranded", "blocker"}
    # Production oracle's room-domain-aware list is authoritative.
    result["recoverable_section_ids"] = ["stranded"]
    assert _recoverable_room_targets(result, sections) == {"stranded"}


@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_room_recovery_moves_unlinked_blocker_when_stranded_teacher_section_is_frozen():
    sections = {
        "core": section("core", "CORE", 540),
        "soft": section("soft", "SOFT", 600),
        "stranded": section("stranded", "LAB1", 540),
        "blocker": section("blocker", "LAB2", 570),
    }

    def rooms(states):
        states = {s.section_id: s for s in states}
        a, b = states["stranded"].meetings[0], states["blocker"].meetings[0]
        overlap = a.day == b.day and a.start_min < b.end_min and b.start_min < a.end_min
        return {
            **room_result(int(overlap)),
            "failures": [{"section_id": "stranded", "reason": "ROOM_OCCUPIED"}] if overlap else [],
            "recoverable_section_ids": ["stranded", "blocker"] if overlap else [],
        }

    result = run(
        sections,
        [pattern(540), pattern(570), pattern(660)],
        rooms,
        teachers={"stranded": frozenset({8})},
        locked={"core", "soft", "stranded"},
    )
    report = result["report"]
    assert (report["room_shortages_before"], report["room_shortages_after"]) == (1, 0)
    assert report["soft_unresolved_before"] == report["soft_unresolved_after"] == 0
    assert report["instructor_before"] == report["instructor_after"]
    assert report["accepted_moves"][0]["section_id"] == "blocker"


@pytest.mark.parametrize("room_target,expected_first", [(None, "soft"), (0, "room_target")])
@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_original_room_floor_prioritizes_congestion_before_unlinked_seat_gain(
    room_target, expected_first
):
    sections = {
        "core": section("core", "CORE", 540),
        "soft": section("soft", "SOFT", 540),
        "room_target": section("room_target", "LAB", 540),
    }

    def rooms(states):
        lab = next(s for s in states if s.section_id == "room_target")
        missing = lab.meetings[0].start_min == 540
        return {
            **room_result(int(missing)),
            "recoverable_section_ids": ["room_target"] if missing else [],
        }

    result = run(
        sections,
        [pattern(540), pattern(600)],
        rooms,
        teachers={"room_target": frozenset({8})},
        room_shortage_target=room_target,
    )
    report = result["report"]
    assert report["accepted_moves"][0]["section_id"] == expected_first
    assert report["room_shortages_after"] == report["soft_unresolved_after"] == 0
    assert report["instructor_before"] == report["instructor_after"]


@pytest.mark.parametrize("protection", ["locked", "online", "contact_duration"])
@override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True)
def test_secondary_recovery_preserves_sections_and_contact_duration(protection):
    sections = {"core": section("core", "CORE", 540), "soft": section("soft", "SOFT", 540)}
    locked = {"core", "soft"} if protection == "locked" else {"core"}
    sections["soft"].delivery_is_online = protection == "online"
    duration = 30 if protection == "contact_duration" else 60
    result = run(
        sections,
        [pattern(540), pattern(600, duration=duration)],
        lambda _states: room_result(0),
        locked=locked,
    )
    assert not result["report"]["applied"]
    assert result["improved_sections"] == list(sections.values())
