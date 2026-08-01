"""Tests for the post-build instructor-schedule compaction pass.

The pass first reduces excess teaching days, then on-campus span/idle, while
strictly guarding students, hard feasibility and room safety. It is flag-gated
(default off). Most tests use a student-free scenario so the instructor
optimisation is exercised directly.
"""

from __future__ import annotations

import itertools
import random

import pytest
from django.test import override_settings

_SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:30", "end": "11:45"},
    {"start": "13:00", "end": "14:15"},
    {"start": "14:30", "end": "15:45"},
]


def test_interval_sweep_matches_randomized_quadratic_oracle():
    from core.services.timetable_instructor_compaction import (
        _sweep_interval_overlaps,
    )

    for seed in range(20):
        rng = random.Random(seed)
        rows = []
        for stable_id in range(80):
            group = rng.choice(("MON", "TUE", "WED"))
            start = rng.randrange(0, 40) * 15
            end = start + rng.randrange(1, 9) * 15
            rows.append((group, start, end, stable_id, stable_id))

        expected = {
            tuple(sorted((left[3], right[3])))
            for left, right in itertools.combinations(rows, 2)
            if left[0] == right[0] and left[1] < right[2] and left[2] > right[1]
        }
        observed = set()
        metrics = _sweep_interval_overlaps(
            rows,
            lambda left, right, target=observed: target.add(tuple(sorted((left, right)))),
        )

        assert observed == expected
        assert metrics["overlap_pairs"] == len(expected)
        assert metrics["candidate_pair_checks"] >= len(expected)


def test_interval_sweep_scales_on_ten_thousand_nonoverlapping_rows():
    from core.services.timetable_instructor_compaction import (
        _sweep_interval_overlaps,
    )

    rows = [("MON", index * 2, index * 2 + 1, index, None) for index in range(10_000)]

    metrics = _sweep_interval_overlaps(rows)

    assert metrics == {
        "rows": 10_000,
        "groups": 1,
        "candidate_pair_checks": 0,
        "overlap_pairs": 0,
        "heap_expirations": 9_999,
        "max_active": 1,
    }


def _gappy_board():
    """One instructor teaching two MON courses with a 2h45 midday hole."""
    from core.models import (
        CourseInstructor,
        DeliveryBoard,
        Instructor,
        Room,
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
        slot_config=_SLOTS,
        lab_slot_config=[],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    Room.objects.create(room_code="R1", capacity=60, department="AI", room_type="lecture")
    instr = Instructor.objects.create(
        full_name="Dr Gap", normalised_name=normalise_instructor("Dr Gap")
    )
    placements = [("C1", "09:00", "10:15"), ("C2", "13:00", "14:15")]  # MON, 2h45 hole
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


def _spread_board(*, online_code: str | None = None):
    """One instructor teaching four one-meeting sections on four days.

    There is no within-day hole, so only a workday-aware search can improve it.
    With a cap of three, the cap/H2 lower bound is two working days.
    """
    from core.models import (
        CourseInstructor,
        DeliveryBoard,
        Instructor,
        ProgrammeRequirement,
        Room,
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
        name="AI M T1 workday compact",
        gender="M",
        programs=["AI"],
        slot_config=_SLOTS,
        lab_slot_config=[],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    Room.objects.create(room_code="R1", capacity=60, department="AI", room_type="lecture")
    instructor = Instructor.objects.create(
        full_name="Dr Spread", normalised_name=normalise_instructor("Dr Spread")
    )
    for index, day in enumerate(("SUN", "MON", "TUE", "THU"), start=1):
        code = f"C{index}"
        is_online = code == online_code
        if is_online:
            ProgrammeRequirement.objects.create(
                program="AI",
                course_code=code,
                course_name=code,
                type="core",
                programme_term=1,
                credit_hours=1,
                is_online=True,
            )
        CourseInstructor.objects.create(
            program="AI", course_code=code, section="M", instructor=instructor, role="primary"
        )
        term_section = TermSection.objects.create(
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
            term_section=term_section,
            day=day,
            start_time="09:00",
            end_time="10:15",
            room="",
            instructor="",
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=term_section,
            day=day,
            start_time="09:00",
            end_time="10:15",
            room="" if is_online else "R1",
            is_locked=False,
        )
        apply_primary_instructor(term_section, scenario, board, term_section.course_code)
    return scenario


def _gappy_lab_board():
    """A movable lab meeting where greedy prefers an undersized lab room."""
    from core.models import (
        Room,
        ScenarioSectionBudget,
        SectionPlacement,
        TermSectionMeeting,
    )

    scenario = _gappy_board()
    for code, end in (("C1", "10:40"), ("C2", "14:40")):
        placement = SectionPlacement.objects.get(
            board__scenario=scenario, term_section__course_code=code
        )
        placement.end_time = end
        placement.save(update_fields=["end_time"])
        TermSectionMeeting.objects.filter(term_section=placement.term_section).update(end_time=end)
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=code,
            course_code=code,
            credit_hours=4,
            planned_sections=1,
            total_demand=60,
            max_per_section=60,
        )
    Room.objects.filter(room_code="R1").update(room_type="lab", capacity=65)
    Room.objects.create(
        room_code="R2",
        capacity=80,
        department="AI",
        room_type="lab",
    )
    return scenario


def _teaching_days(scenario) -> set[str]:
    from core.models import SectionPlacement

    return set(
        SectionPlacement.objects.filter(board__scenario=scenario).values_list("day", flat=True)
    )


def test_metrics_count_online_as_work_but_not_campus_idle() -> None:
    from core.services.timetable_assignment_models import SectionMeeting, SectionState
    from core.services.timetable_instructor_compaction import (
        _compute_instructor_compaction_metrics,
    )

    def section(sid, meetings, *, online=False):
        return SectionState(
            section_id=sid,
            course_code=sid,
            meetings=[SectionMeeting(*meeting) for meeting in meetings],
            max_capacity=30,
            reserve_capacity=0,
            is_online=online,
        )

    sections = {
        "A": section("A", [(0, 540, 615)]),
        "B": section("B", [(0, 660, 720), (1, 540, 615)], online=True),
        "C": section("C", [(0, 840, 915)]),
    }
    metrics = _compute_instructor_compaction_metrics(
        sections,
        {sid: frozenset({7}) for sid in sections},
        3,
    )
    row = metrics["per_instructor"][7]

    assert row["session_count"] == 4
    assert row["working_days"] == 2  # the online-only MON still counts as work
    assert row["lower_bound_days"] == 2
    assert row["campus_days"] == 1
    assert row["physical_idle"] == 225  # 14:00 - 10:15; online is not a bridge
    assert row["physical_span"] == 375


def test_lower_bound_also_respects_h2_distinct_section_days() -> None:
    from core.services.timetable_assignment_models import SectionMeeting, SectionState
    from core.services.timetable_instructor_compaction import (
        _compute_instructor_compaction_metrics,
    )

    section = SectionState(
        section_id="A",
        course_code="A",
        meetings=[
            SectionMeeting(0, 540, 615),
            SectionMeeting(1, 540, 615),
            SectionMeeting(2, 540, 615),
        ],
        max_capacity=30,
        reserve_capacity=0,
    )
    row = _compute_instructor_compaction_metrics({"A": section}, {"A": frozenset({7})}, 3)[
        "per_instructor"
    ][7]

    assert row["lower_bound_days"] == 3  # not ceil(3/3) == 1: H2 also binds


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
    TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED=True,
)
def test_compaction_reduces_excess_working_days_without_existing_holes() -> None:
    from core.models import SectionPlacement
    from core.services.timetable_constraints import (
        count_instructor_clashes,
        count_instructor_daily_overloads,
    )
    from core.services.timetable_instructor_compaction import compact_instructor_schedules
    from core.services.timetable_optimizer_v2 import (
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
    )

    scenario = _spread_board()
    assert len(_teaching_days(scenario)) == 4

    report = compact_instructor_schedules(scenario.id)

    impact = report["instructor_impact"]
    assert impact["working_days_before"] == 4
    assert impact["lower_bound_days"] == 2
    assert impact["total_excess_days_before"] == 2
    assert impact["working_days_after"] == 2
    assert impact["total_excess_days_after"] == 0
    assert impact["working_days_worsened"] == 0
    assert report["persistence"]["committed"] is True
    assert len(_teaching_days(scenario)) == 2
    assert not SectionPlacement.objects.filter(board__scenario=scenario, room="").exists()

    states = build_section_states_for_scenario(scenario.id)
    by_id = {state.section_id: state for state in states}
    instructor_map = build_section_instructor_map_for_scenario(scenario.id)
    assert count_instructor_clashes(by_id, instructor_map) == 0
    assert count_instructor_daily_overloads(by_id, instructor_map, 3) == 0
    assert report["protected"]["feasibility_after"] == report["protected"]["feasibility_before"]


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
    TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED=True,
)
def test_online_meeting_counts_for_workdays_but_not_physical_days() -> None:
    from core.models import SectionPlacement
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _spread_board(online_code="C4")
    report = compact_instructor_schedules(scenario.id)
    impact = report["instructor_impact"]
    instructor = impact["per_instructor"][0]

    assert instructor["session_count"] == 4
    assert instructor["working_days_before"] == 4
    assert instructor["campus_days_before"] == 3
    assert impact["working_days_after"] == 2
    assert report["persistence"]["committed"] is True
    online = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C4")
    assert online.room == ""  # online is intentionally not roomed


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_compaction_rolls_back_if_moved_physical_meeting_cannot_be_roomed() -> None:
    from core.models import Room, SectionPlacement
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _spread_board()
    before = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("term_section__course_code")
        .values_list("term_section__course_code", "day", "start_time", "room")
    )
    Room.objects.all().delete()

    report = compact_instructor_schedules(scenario.id)
    after = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("term_section__course_code")
        .values_list("term_section__course_code", "day", "start_time", "room")
    )

    assert report["persistence"]["committed"] is False
    assert report["persistence"]["rolled_back"] is True
    assert "could not assign every moved physical meeting" in report["persistence"]["reason"]
    assert report["persistence"]["exact_room_fallback"]["status"] == "infeasible"
    assert report["persistence"]["exact_room_fallback"]["proven_optimal"] is False
    assert report["persistence"]["exact_room_fallback"]["rolled_back"] is True
    assert report["search"]["moves_accepted"] == 0
    assert report["relocations"] == []
    assert after == before


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
def test_compaction_noop_when_flag_off() -> None:
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    report = compact_instructor_schedules(scenario.id)
    assert report == {"enabled": False}
    assert _instructor_idle(scenario) == 165  # untouched when off


@pytest.mark.django_db(transaction=True)
@override_settings(TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True)
def test_shared_multi_board_section_fails_closed_without_writes() -> None:
    from core.models import DeliveryBoard, SectionPlacement
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    original = SectionPlacement.objects.filter(
        board__scenario=scenario, term_section__course_code="C1"
    ).get()
    replica_board = DeliveryBoard.objects.create(
        scenario=scenario, label="T2", nominal_term=2, program="AI"
    )
    SectionPlacement.objects.create(
        board=replica_board,
        term_section=original.term_section,
        day=original.day,
        start_time=original.start_time,
        end_time=original.end_time,
        room=original.room,
    )
    before = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("id")
        .values_list("id", "board_id", "day", "start_time", "end_time", "room")
    )

    report = compact_instructor_schedules(scenario.id)

    after = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("id")
        .values_list("id", "board_id", "day", "start_time", "end_time", "room")
    )
    assert report["persistence"]["skipped"] is True
    assert report["persistence"]["rolled_back"] is False
    assert report["coverage"]["shared_multi_board_sections"] == 1
    assert report["coverage"]["duplicate_logical_meeting_keys"] == 1
    assert report["search"]["moves_evaluated"] == 0
    assert len(report["unsupported_shared_sections"]) == 1
    assert after == before


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_empty_slot_configs_use_authoritative_defaults() -> None:
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _spread_board()
    scenario.slot_config = []
    scenario.lab_slot_config = []
    scenario.save(update_fields=["slot_config", "lab_slot_config"])

    report = compact_instructor_schedules(scenario.id)

    assert report["persistence"]["committed"] is True
    assert report["instructor_impact"]["working_days_before"] == 4
    assert report["instructor_impact"]["working_days_after"] == 2


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_custom_grid_never_changes_h1_meeting_duration() -> None:
    from core.models import SectionPlacement
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    scenario.slot_config = [
        {"start": "09:00", "end": "10:30"},
        {"start": "10:45", "end": "12:15"},
    ]
    scenario.save(update_fields=["slot_config"])
    before = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("id")
        .values_list("day", "start_time", "end_time")
    )

    report = compact_instructor_schedules(scenario.id)

    after = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("id")
        .values_list("day", "start_time", "end_time")
    )
    assert report["search"]["moves_accepted"] == 0
    assert after == before
    assert all(
        (int(end[:2]) * 60 + int(end[3:])) - (int(start[:2]) * 60 + int(start[3:])) == 75
        for _day, start, end in after
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("room_field", "room_value", "section_value", "constraint"),
    [
        ("room_type", "lab", None, "H11"),
        ("capacity", 39, None, "H12"),
        ("section", "F", "M1", "H13"),
        ("department", "DS", None, "H14"),
    ],
)
def test_moved_room_validator_checks_each_h11_to_h14_rule(
    room_field: str,
    room_value: object,
    section_value: str | None,
    constraint: str,
) -> None:
    from core.models import Room, SectionPlacement
    from core.services.timetable_instructor_compaction import (
        _validate_moved_room_compatibility,
    )

    scenario = _gappy_board()
    placement = SectionPlacement.objects.filter(board__scenario=scenario).first()
    assert placement is not None
    if section_value is not None:
        placement.term_section.section = section_value
        placement.term_section.save(update_fields=["section"])
    Room.objects.filter(room_code="R1").update(**{room_field: room_value})

    result = _validate_moved_room_compatibility(scenario.id, {placement.id})

    assert result["valid"] is False
    assert result["checked_count"] == 1
    assert constraint in result["violations"][0]["failed_constraints"]


@pytest.mark.django_db(transaction=True)
def test_lab_h12_uses_buffered_budget_demand() -> None:
    from core.models import (
        Room,
        ScenarioSectionBudget,
        SectionPlacement,
        TermSectionMeeting,
    )
    from core.services.timetable_instructor_compaction import (
        _validate_moved_room_compatibility,
    )

    scenario = _gappy_board()
    placement = SectionPlacement.objects.get(
        board__scenario=scenario, term_section__course_code="C1"
    )
    placement.end_time = "10:40"
    placement.save(update_fields=["end_time"])
    TermSectionMeeting.objects.filter(term_section=placement.term_section).update(end_time="10:40")
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="C1",
        course_code="C1",
        credit_hours=4,
        planned_sections=1,
        total_demand=60,
        max_per_section=60,
    )
    Room.objects.filter(room_code="R1").update(room_type="lab", capacity=65)

    rejected = _validate_moved_room_compatibility(scenario.id, {placement.id})
    assert rejected["checks"][0]["required_type"] == "lab"
    assert rejected["checks"][0]["buffered_demand"] == 66
    assert "H12" in rejected["violations"][0]["failed_constraints"]

    Room.objects.filter(room_code="R1").update(capacity=66)
    accepted = _validate_moved_room_compatibility(scenario.id, {placement.id})
    assert accepted["valid"] is True


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_exact_fallback_replaces_greedy_undersized_lab_with_feasible_room() -> None:
    from core.models import SectionPlacement, TermSectionMeeting
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_lab_board()
    report = compact_instructor_schedules(scenario.id)

    persistence = report["persistence"]
    assert persistence["committed"] is True
    assert persistence["greedy_room_validation"]["valid"] is False
    assert any(
        "H12" in violation["failed_constraints"]
        for violation in persistence["greedy_room_validation"]["violations"]
    )
    fallback = persistence["exact_room_fallback"]
    assert fallback["status"] == "optimal"
    assert fallback["proven_optimal"] is True
    assert fallback["scope"] == "moved_only"
    assert fallback["objective"]["room_changes"] >= 1
    assert persistence["room_validation"]["valid"] is True
    moved_ids = {int(placement_id) for placement_id in fallback["assignment"]}
    assert moved_ids
    assert set(
        SectionPlacement.objects.filter(id__in=moved_ids).values_list("room", flat=True)
    ) == {"R2"}
    assert set(
        TermSectionMeeting.objects.filter(
            term_section_id__in=SectionPlacement.objects.filter(id__in=moved_ids).values_list(
                "term_section_id", flat=True
            )
        ).values_list("room", flat=True)
    ) == {"R2"}


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_exact_room_fallback_is_deterministic() -> None:
    from core.models import SectionPlacement
    from core.services.timetable_instructor_compaction import (
        _exact_repair_affected_rooms,
        compact_instructor_schedules,
    )

    scenario = _gappy_lab_board()
    report = compact_instructor_schedules(scenario.id)
    moved_ids = {
        int(placement_id)
        for placement_id in report["persistence"]["exact_room_fallback"]["assignment"]
    }
    preferred = {placement_id: "R1" for placement_id in moved_ids}

    SectionPlacement.objects.filter(id__in=moved_ids).update(room="R1")
    first = _exact_repair_affected_rooms(scenario.id, moved_ids, preferred)
    SectionPlacement.objects.filter(id__in=moved_ids).update(room="R1")
    second = _exact_repair_affected_rooms(scenario.id, moved_ids, preferred)

    assert first["status"] == second["status"] == "optimal"
    assert first["scope"] == second["scope"]
    assert first["assignment"] == second["assignment"]
    assert first["objective"] == second["objective"]


@pytest.mark.django_db(transaction=True)
def test_exact_fallback_expands_to_overlap_component_for_required_room_swap() -> None:
    from core.models import (
        Room,
        ScenarioSectionBudget,
        SectionPlacement,
        TermSectionMeeting,
    )
    from core.services.timetable_instructor_compaction import (
        _exact_repair_affected_rooms,
    )

    scenario = _gappy_board()
    small = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C1")
    large = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C2")
    small.end_time = "10:40"
    small.room = "R2"
    small.save(update_fields=["end_time", "room"])
    large.day = "MON"
    large.start_time = "09:00"
    large.end_time = "10:40"
    large.room = "R1"
    large.save(update_fields=["day", "start_time", "end_time", "room"])
    for placement in (small, large):
        TermSectionMeeting.objects.filter(term_section=placement.term_section).update(
            day=placement.day,
            start_time=placement.start_time,
            end_time=placement.end_time,
            room=placement.room,
        )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="C1",
        course_code="C1",
        credit_hours=4,
        planned_sections=1,
        total_demand=60,
        max_per_section=60,
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="C2",
        course_code="C2",
        credit_hours=4,
        planned_sections=1,
        total_demand=90,
        max_per_section=90,
    )
    Room.objects.filter(room_code="R1").update(room_type="lab", capacity=70)
    Room.objects.create(
        room_code="R2",
        capacity=100,
        department="AI",
        room_type="lab",
    )

    result = _exact_repair_affected_rooms(
        scenario.id,
        {large.id},
        {small.id: "R2", large.id: "R1"},
    )

    assert result["status"] == "optimal"
    assert result["scope"] == "overlap_component"
    assert result["attempts"][0]["scope"] == "moved_only"
    assert result["attempts"][0]["status"] == "infeasible_empty_domain"
    small.refresh_from_db()
    large.refresh_from_db()
    assert small.room == "R1"
    assert large.room == "R2"


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_successful_persistence_syncs_meeting_time_and_room() -> None:
    from core.models import SectionPlacement, TermSectionMeeting
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _spread_board()
    report = compact_instructor_schedules(scenario.id)

    assert report["persistence"]["committed"] is True
    assert report["persistence"]["meeting_sync"]["sections_synced"] == 4
    assert all(check["matches"] for check in report["persistence"]["meeting_mapping_checks"])
    for term_section_id in {
        row[0]
        for row in SectionPlacement.objects.filter(board__scenario=scenario).values_list(
            "term_section_id"
        )
    }:
        placements = set(
            SectionPlacement.objects.filter(term_section_id=term_section_id).values_list(
                "day", "start_time", "end_time", "room"
            )
        )
        meetings = set(
            TermSectionMeeting.objects.filter(term_section_id=term_section_id).values_list(
                "day", "start_time", "end_time", "room"
            )
        )
        assert meetings == placements


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_missing_meeting_sync_mapping_rolls_back(monkeypatch) -> None:
    from types import SimpleNamespace

    from core.models import SectionPlacement, TermSectionMeeting
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _spread_board()
    before_placements = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("id")
        .values_list("id", "day", "start_time", "end_time", "room")
    )
    before_meetings = list(
        TermSectionMeeting.objects.filter(term_section__scenario=scenario)
        .order_by("id")
        .values_list("id", "day", "start_time", "end_time", "room", "instructor")
    )

    def no_sync(_scenario_id):
        return SimpleNamespace(sections_synced=0, meetings_written=0, meetings_deleted=0)

    monkeypatch.setattr(
        "core.services.timetable_board_persistence.sync_meetings_from_placements",
        no_sync,
    )
    report = compact_instructor_schedules(scenario.id)

    assert report["persistence"]["rolled_back"] is True
    assert "did not produce the expected placement mapping" in report["persistence"]["reason"]
    assert report["persistence"]["meeting_sync_rolled_back"] is True
    assert (
        list(
            SectionPlacement.objects.filter(board__scenario=scenario)
            .order_by("id")
            .values_list("id", "day", "start_time", "end_time", "room")
        )
        == before_placements
    )
    assert (
        list(
            TermSectionMeeting.objects.filter(term_section__scenario=scenario)
            .order_by("id")
            .values_list("id", "day", "start_time", "end_time", "room", "instructor")
        )
        == before_meetings
    )


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_rooming_exception_rolls_back_and_discards_telemetry(monkeypatch) -> None:
    from core.models import SectionPlacement
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _spread_board()
    before = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("id")
        .values_list("id", "day", "start_time", "end_time", "room")
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic rooming failure")

    monkeypatch.setattr("core.services.timetable_rooming.assign_rooms_to_board", explode)
    report = compact_instructor_schedules(scenario.id)

    assert report["persistence"]["rolled_back"] is True
    assert report["persistence"]["exception_type"] == "RuntimeError"
    assert "synthetic rooming failure" in report["persistence"]["reason"]
    assert report["persistence"]["rooming"] == {}
    assert report["persistence"]["rooming_rolled_back"] is True
    assert report["persistence"]["rooming_results_discarded"] is True
    assert (
        list(
            SectionPlacement.objects.filter(board__scenario=scenario)
            .order_by("id")
            .values_list("id", "day", "start_time", "end_time", "room")
        )
        == before
    )


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_multi_move_persistence_vacates_unique_keys_before_final_writes(
    monkeypatch,
) -> None:
    from django.db.models.query import QuerySet

    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _spread_board()
    original_update = QuerySet.update
    staged_days: list[str] = []

    def update_spy(self, **kwargs):
        day = str(kwargs.get("day", ""))
        if day.startswith("__COMPACTION_STAGE_"):
            staged_days.append(day)
        return original_update(self, **kwargs)

    monkeypatch.setattr(QuerySet, "update", update_spy)
    report = compact_instructor_schedules(scenario.id)

    assert report["persistence"]["committed"] is True
    assert len(report["persistence"]["expected_update_mapping"]) >= 2
    assert len(staged_days) == len(report["persistence"]["expected_update_mapping"])
    assert len(set(staged_days)) == len(staged_days)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "regressed_key",
    [
        "h15_critical_overlaps",
        "h7_instructor_clashes",
        "h8_daily_overload_sessions",
        "h9_room_clashes",
        "physical_unassigned_rooms",
    ],
)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_each_persisted_hard_counter_is_an_independent_rollback_gate(
    monkeypatch,
    regressed_key: str,
) -> None:
    from core.services import timetable_instructor_compaction as compaction

    scenario = _spread_board()
    baseline = {
        "h15_critical_overlaps": 0,
        "h7_instructor_clashes": 0,
        "h8_daily_overload_sessions": 0,
        "h9_room_clashes": 0,
        "physical_unassigned_rooms": 0,
    }
    after = dict(baseline)
    after[regressed_key] = 1
    results = iter((baseline, after))
    monkeypatch.setattr(
        compaction,
        "_scenario_persistence_hard_metrics",
        lambda _scenario_id, _cap: next(results),
    )

    report = compaction.compact_instructor_schedules(scenario.id)

    assert report["persistence"]["rolled_back"] is True
    assert f"{regressed_key} increased 0->1" in report["persistence"]["reason"]
    assert report["search"]["moves_accepted"] == 0
    assert report["relocations"] == []


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
    TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS=0.000000001,
)
def test_timeout_is_reported_when_budget_expires_before_first_round(monkeypatch) -> None:
    from types import SimpleNamespace

    from core.services import timetable_instructor_compaction as compaction

    scenario = _spread_board()
    calls = 0

    def monotonic() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 1.0

    monkeypatch.setattr(compaction, "time", SimpleNamespace(monotonic=monotonic))
    report = compaction.compact_instructor_schedules(scenario.id)

    assert report["search"]["timed_out"] is True
    assert report["search"]["rounds_used"] == 0
    assert report["search"]["moves_accepted"] == 0
    assert report["search"]["elapsed_seconds"] > 0
