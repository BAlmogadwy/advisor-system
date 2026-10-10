"""Whole-week English bundles, independently checked across scheduler stages."""

from dataclasses import replace

import pytest

from core.services.timetable_course_policy import get_course_meeting_policy, meeting_signature
from scheduler.domain import (
    CapacityPolicy,
    DeliveryMode,
    Grid,
    Instructor,
    MeetingKind,
    MeetingRequirement,
    Offering,
    Section,
    Snapshot,
    StudentDemand,
    TimeWindow,
)
from scheduler.domain.board import Board, Placement
from scheduler.domain.calendar import Day
from scheduler.instructors import assign_instructors, section_min_days
from scheduler.meeting_patterns import pattern_cells
from scheduler.placement import place_naively
from scheduler.readiness import assess
from scheduler.seating import seat_students
from scheduler.solve import instructor_metrics, solve, time_of_day_drift
from scheduler.validate import validate


def english(code="ENG101"):
    policy = get_course_meeting_policy(code)
    return Offering(
        "eng",
        code,
        "English language skills",
        4,
        frozenset({"AI"}),
        frozenset({1}),
        (
            MeetingRequirement(
                MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 180, 4, uses_shared_room=False
            ),
        ),
        25,
        True,
        meeting_policy=policy,
    )


def snapshot(*, count=1, instructor=None, code="ENG101", grid=None):
    return Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=grid
        or Grid.from_spec(lecture_starts={"09:00": 75, "13:00": 75}, lab_starts={"09:00": 100}),
        offerings=(english(code),),
        sections=tuple(
            Section(f"eng#S{i}", "eng", i, 25, instructor_id=instructor)
            for i in range(1, count + 1)
        ),
        rooms=(),
        instructors=(Instructor(instructor, "Teacher", frozenset({"eng"})),)
        if instructor is not None
        else (),
        demand=(),
        policy=CapacityPolicy(25),
        source_fingerprint="same-source",
        created_at="2026-10-10T00:00:00Z",
    )


def bundle(snap, section_index=0, pattern_index=0):
    section = snap.sections[section_index]
    policy = snap.offerings_by_id[section.offering_id].meeting_policy
    return tuple(
        Placement(
            section.id,
            section.offering_id,
            index,
            MeetingKind.LECTURE,
            DeliveryMode.IN_PERSON,
            day,
            window,
            None,
            section.instructor_id,
            uses_shared_room=False,
        )
        for index, (day, window) in enumerate(pattern_cells(policy.patterns[pattern_index]), 1)
    )


def violations(snap, board, rule_id):
    return next(r for r in validate(snap, board).results if r.rule_id == rule_id).violations


@pytest.mark.parametrize("code", ["ENG101", "ENG102"])
def test_solver_selects_one_complete_bundle_per_parallel_section(code):
    snap = snapshot(count=4, code=code)
    result = solve(
        snap,
        time_limit_seconds=3,
        workers=1,
        exact_block_tiers=frozenset({"T1"}),
        max_time_of_day_slots=0,
        sibling_adjacency_weight=1000,
    )
    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert not result.unplaced
    assert not validate(snap, result.board).violated
    assert len(result.board.placements) == 16
    assert not result.board.unroomed
    for section in snap.sections:
        rows = result.board.by_section[section.id]
        assert snap.offerings[0].meeting_policy.accepts(rows)
        assert {p.day for p in rows} == {Day.SUN, Day.MON, Day.WED, Day.THU}
        assert sum(p.window.duration for p in rows) == 720
    assert time_of_day_drift(snap, result.board)["percent_same_slot"] == 100.0


def test_teacher_can_teach_two_opposite_bundles_but_not_three():
    snap = snapshot(count=3, instructor=7)
    assigned = assign_instructors(snap)
    assert [s.instructor_id for s in assigned.sections] == [7, 7, None]
    assert section_min_days(assigned.offerings[0]) == 4
    result = solve(assigned, time_limit_seconds=3, workers=1, max_working_days=4)
    assert result.status in {"OPTIMAL", "FEASIBLE"}
    starts = [
        set(p.window.start for p in result.board.by_section[s.id]) for s in assigned.sections[:2]
    ]
    assert {frozenset(values) for values in starts} == {frozenset({540}), frozenset({780})}
    metrics = instructor_metrics(assigned, result.board)
    assert metrics["working_days"] == 4
    assert metrics["idle_minutes"] == 240
    assert not validate(assigned, result.board).violated


def test_greedy_places_opposite_teacher_bundles_atomically():
    snap = snapshot(count=2, instructor=7)
    board = place_naively(snap)
    assert {p.window.start for p in board.by_section["eng#S1"]} == {540}
    assert {p.window.start for p in board.by_section["eng#S2"]} == {780}
    assert not validate(snap, board).violated


def test_both_intensive_courses_share_the_teachers_two_bundle_capacity():
    snap = snapshot(count=2, instructor=7)
    second = replace(english("ENG102"), id="eng2")
    snap = replace(
        snap,
        offerings=(*snap.offerings, second),
        sections=(*snap.sections, Section("eng2#S1", "eng2", 1, 25)),
        instructors=(Instructor(7, "Teacher", frozenset({"eng", "eng2"})),),
    )
    assigned = assign_instructors(snap)
    assert [s.instructor_id for s in assigned.sections] == [7, 7, None]
    assert solve(assigned, time_limit_seconds=3, workers=1).status in {"OPTIMAL", "FEASIBLE"}
    one_per_day = assign_instructors(snap, daily_cap=1)
    assert [s.instructor_id for s in one_per_day.sections] == [7, None, None]


@pytest.mark.django_db
def test_saved_pattern_retains_four_continuous_lectures_and_policy_fingerprint():
    from scheduler.models import SchedulerPlacement
    from scheduler.persist import save_plan

    snap = snapshot()
    board = Board(bundle(snap, pattern_index=1))
    stored = save_plan(
        snap,
        board,
        config={"policy": "ENG_INTENSIVE_V1"},
        solver_status="OPTIMAL",
        wall_time_seconds=0.1,
        certification="UNCERTIFIED",
        violation_count=0,
        expected_clashes=0,
        naive_baseline=0,
        instructors=instructor_metrics(snap, board),
        rooms={},
        pairing={},
    )
    rows = list(SchedulerPlacement.objects.filter(plan=stored).order_by("meeting_index"))
    assert stored.snapshot_fingerprint == snap.fingerprint()
    assert [r.day for r in rows] == ["SUN", "MON", "WED", "THU"]
    assert {(r.start_minute, r.end_minute, r.kind, r.delivery, r.room_id) for r in rows} == {
        (780, 960, "LECTURE", "IN_PERSON", ""),
    }


def test_greedy_leaves_whole_bundle_missing_when_each_pattern_is_blocked():
    snap = snapshot(instructor=7)
    ordinary = tuple(
        Offering(
            oid,
            oid,
            oid,
            1,
            frozenset({"AI"}),
            frozenset({1}),
            (
                MeetingRequirement(
                    MeetingKind.LECTURE,
                    DeliveryMode.IN_PERSON,
                    75,
                    1,
                    allowed_starts=frozenset({start}),
                    uses_shared_room=False,
                ),
            ),
            40,
            True,
        )
        for oid, start in (("a", 540), ("b", 780))
    )
    snap = replace(
        snap,
        offerings=(*ordinary, *snap.offerings),
        sections=(
            Section("a#S1", "a", 1, 40, instructor_id=7),
            Section("b#S1", "b", 1, 40, instructor_id=7),
            *snap.sections,
        ),
    )
    board = place_naively(snap)
    assert len(board.placements) == 2
    assert "eng#S1" not in board.by_section
    assert violations(snap, board, "H1")


def test_mixed_daily_options_fail_whole_week_certification_and_fixed_solve():
    snap = snapshot()
    mixed = list(bundle(snap))
    mixed[1] = replace(mixed[1], window=TimeWindow(780, 960))
    board = Board(tuple(mixed))
    assert not violations(snap, board, "H1")
    assert not violations(snap, board, "H3")
    assert violations(snap, board, "H_WEEKLY_PATTERN")
    result = solve(snap, fix=board, time_limit_seconds=3, workers=1)
    assert result.status == "INFEASIBLE"
    assert not result.board.placements


@pytest.mark.parametrize(
    "change",
    [
        {"day": Day.TUE},
        {"window": TimeWindow(540, 615)},
        {"window": TimeWindow(555, 735)},
        {"day": Day.MON},
    ],
)
def test_illegal_day_shortened_shifted_or_duplicate_meeting_is_rejected(change):
    snap = snapshot()
    rows = list(bundle(snap))
    rows[0] = replace(rows[0], **change)
    board = Board(tuple(rows))
    assert violations(snap, board, "H_WEEKLY_PATTERN")
    assert validate(snap, board).violated


def test_partial_bundle_does_not_offer_a_deliverable_student_seat():
    snap = replace(snapshot(), demand=(StudentDemand(1, "AI", frozenset({"eng"})),))
    good = seat_students(snap, Board(bundle(snap)), time_limit_seconds=3)
    assert good.seated_demands == 1
    assert good.unseated_demands == 0
    for rows in (
        bundle(snap)[:3],
        (*bundle(snap)[:3], replace(bundle(snap)[3], window=TimeWindow(780, 960))),
    ):
        bad = seat_students(snap, Board(tuple(rows)), time_limit_seconds=3)
        assert bad.unseated_demands == 1
        assert bad.seated_demands == 0


def test_continuous_bundle_occupies_ordinary_changeover_interval():
    grid = Grid.from_spec(lecture_starts={"10:15": 15}, lab_starts={})
    snap = snapshot(instructor=7, grid=grid)
    ordinary = Offering(
        "a",
        "AI123",
        "Ordinary",
        1,
        frozenset({"AI"}),
        frozenset({1}),
        (
            MeetingRequirement(
                MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 15, 1, uses_shared_room=False
            ),
        ),
        25,
        True,
    )
    snap = replace(
        snap,
        offerings=(*snap.offerings, ordinary),
        sections=(*snap.sections, Section("a#S1", "a", 1, 25, instructor_id=7)),
        demand=(StudentDemand(1, "AI", frozenset({"eng", "a"})),),
    )
    short = Placement(
        "a#S1",
        "a",
        1,
        MeetingKind.LECTURE,
        DeliveryMode.IN_PERSON,
        Day.SUN,
        TimeWindow(615, 630),
        None,
        7,
        uses_shared_room=False,
    )
    fixed = Board((*bundle(snap), short))
    assert violations(snap, fixed, "H7")
    assert solve(snap, fix=fixed, time_limit_seconds=3, workers=1).status == "INFEASIBLE"
    result = solve(
        snap,
        fix=Board((short,)),
        free_sections=frozenset({"eng#S1"}),
        time_limit_seconds=3,
        workers=1,
    )
    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert {p.window.start for p in result.board.by_section["eng#S1"]} == {780}
    seating = seat_students(snap, fixed, time_limit_seconds=3)
    assert seating.total_clashes == 1


def test_policy_is_scoped_and_fingerprinted_with_no_shared_room_shortage():
    snap = snapshot(count=10)
    assert not snap.grid.windows_for(180)
    assert not assess(snap).blocking
    assert not snap.physical_meetings_required()
    assert solve(snap, time_limit_seconds=3, workers=1).status in {"OPTIMAL", "FEASIBLE"}
    policy = snap.offerings[0].meeting_policy
    altered = replace(
        snap,
        offerings=(
            replace(
                snap.offerings[0], meeting_policy=replace(policy, patterns=(policy.patterns[0],))
            ),
        ),
    )
    assert altered.source_fingerprint == snap.source_fingerprint
    assert altered.fingerprint() != snap.fingerprint()
    ordinary = replace(snap.offerings[0], course_code="ENGL103", meeting_policy=None)
    normal = replace(snap, offerings=(ordinary,))
    assert violations(normal, Board(bundle(snap)), "H3")
    assert assess(normal).blocking


def test_bridge_keeps_intensive_lecture_in_lecture_columns():
    from scheduler.bridge import grid_columns_for

    snap = snapshot()
    lectures, labs, added, lab_added = grid_columns_for(
        Board(bundle(snap, pattern_index=1)),
        [],
        [],
        lecture_section_ids=frozenset({"eng#S1"}),
    )
    assert [row for row in lectures if row.get("policy_only")] == [
        {"label": "13:00-16:00", "start": "13:00", "end": "16:00", "policy_only": True}
    ]
    assert labs == []
    assert (added, lab_added) == (1, 0)


@pytest.mark.django_db
@pytest.mark.parametrize("pattern_index", [0, 1])
@pytest.mark.parametrize("empty_grid", [False, True])
def test_saved_english_display_columns_never_expand_ordinary_course_options(
    pattern_index, empty_grid
):
    from core.models import (
        DeliveryBoard,
        ScenarioSectionBudget,
        SectionPlacement,
        TimetableScenario,
    )
    from core.services.timetable_autoplace import (
        DEFAULT_SLOTS,
        auto_place_board,
        generate_course_meeting_options,
    )
    from scheduler.bridge import grid_columns_for

    snap = snapshot()
    columns, _, _, _ = grid_columns_for(
        Board(bundle(snap, pattern_index=pattern_index)),
        [] if empty_grid else DEFAULT_SLOTS,
        [],
        lecture_section_ids=frozenset({"eng#S1"}),
    )
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", slot_config=columns)
    scenario.refresh_from_db()
    options = generate_course_meeting_options("AI123", 3, scenario.slot_config)
    assert options
    assert all(
        int(m["end"][:2]) * 60 + int(m["end"][3:]) - int(m["start"][:2]) * 60 - int(m["start"][3:])
        == 75
        for option in options
        for m in option
    )
    rebuilt, _, added, _ = grid_columns_for(
        Board(bundle(snap, pattern_index=pattern_index)),
        scenario.slot_config,
        [],
        lecture_section_ids=frozenset({"eng#S1"}),
    )
    assert rebuilt == columns and added == 0
    assert len(generate_course_meeting_options("ENG101", 4, rebuilt)) == 2
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        programme_term=1,
        course_code="AI123",
        course_key="AI123",
        credit_hours=3,
        total_demand=25,
        planned_sections=1,
        max_per_section=25,
    )
    auto_place_board(board.pk)
    rows = list(SectionPlacement.objects.filter(board=board))
    assert len(rows) == 2
    assert all(meeting_signature(row)[2] - meeting_signature(row)[1] == 75 for row in rows)


def test_bridge_preserves_explicit_existing_ordinary_columns():
    from scheduler.bridge import grid_columns_for

    snap = snapshot()
    existing = [{"label": "Custom lecture", "start": "09:00", "end": "12:00"}]
    lectures, _, added, _ = grid_columns_for(
        Board(bundle(snap)), existing, [], lecture_section_ids=frozenset({"eng#S1"})
    )
    assert lectures == existing and added == 0
