"""The clean-room scheduler honours per-instructor teaching-time rules (slice D).

A rule (``InstructorTimeRule``) reaches ``scheduler/`` as ``Instructor.forbidden``:
merged ``(day_index, start, end)`` windows the instructor may not teach in. The
solver treats them as HARD, the instructor assignment never gives a ruled
instructor a section he cannot hold, the validator grades them, and a rule never
costs a class its place or fails a build. With the switch off, or no rule stored,
nothing changes at all.

Most tests here are pure (in-memory snapshots); the intake tests need the database.
Every test states the behaviour it protects; the oracle for "is this meeting in
forbidden time" is written out here and is deliberately not the code's helper.
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import CourseInstructor, InstructorTimeRule, ProgrammeRequirement, Room, Student
from core.models import Instructor as InstructorRow
from core.services.instructor_availability import (
    TimeRule,
    compile_rules,
    rules_in_force,
)
from core.services.timetable_course_policy import get_course_meeting_policy
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
    TimeWindow,
)
from scheduler.domain.board import Board, Placement
from scheduler.domain.calendar import Day
from scheduler.instructors import assign_instructors
from scheduler.intake import IntakeError, build_snapshot
from scheduler.placement import place_naively
from scheduler.readiness import assess
from scheduler.rules import RULEBOOK
from scheduler.solve import _portfolio_finalists, plan, plan_portfolio, solve
from scheduler.time_rules import (
    describe_time_rule_cost,
    has_legal_week,
    release_instructors,
    restricted_instructor_ids,
    time_rule_cost,
)
from scheduler.validate import Certification, validate

RULE_ID = "H_INSTRUCTOR_TIME"

GRID = Grid.from_spec(
    lecture_starts={"09:00": 75, "10:30": 75, "13:00": 75},
    lab_starts={"09:00": 100, "10:45": 100},
    online_starts={"15:00": 75, "16:45": 75},
)
W9 = TimeWindow(540, 615)  # 09:00-10:15
W1030 = TimeWindow(630, 705)  # 10:30-11:45
W13 = TimeWindow(780, 855)  # 13:00-14:15
SUN, MON, TUE, WED, THU = (Day.SUN, Day.MON, Day.TUE, Day.WED, Day.THU)
CELLS_PER_DAY = (W9, W1030, W13)  # the in-person 75-minute family, per day


# ── builders ──────────────────────────────────────────────────────────────


def offering(oid="o1", *, meetings=1, delivery=DeliveryMode.IN_PERSON, duration=75, policy=None):
    if policy is not None:
        reqs = (MeetingRequirement(MeetingKind.LECTURE, delivery, 180, 4, uses_shared_room=False),)
    else:
        reqs = (MeetingRequirement(MeetingKind.LECTURE, delivery, duration, meetings),)
    return Offering(
        id=oid,
        course_code=oid.upper(),
        course_name=oid,
        credit_hours=3,
        programs=frozenset({"AI"}),
        terms=frozenset({1}),
        requirements=reqs,
        capacity=30,
        capacity_is_declared=True,
        meeting_policy=policy,
    )


def snapshot(offerings, sections, instructors=(), grid=GRID):
    return Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=grid,
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=(),
        instructors=tuple(instructors),
        demand=(),
        policy=CapacityPolicy(default_capacity=25),
        source_fingerprint="test",
        created_at="2026-11-22T00:00:00+00:00",
    )


def allowed_only(*cells):
    """``forbidden`` of an instructor who may teach ONLY ``cells`` = ``(day, start, end)``."""
    rules = [TimeRule(1, "available_only", 1 << day, start, end) for day, start, end in cells]
    availability = compile_rules(rules)
    return tuple(
        (day, start, end)
        for day in range(5)
        for start, end in (availability.forbidden(1, day) if availability else ())
    )


def cell(day: Day, window: TimeWindow):
    return (day.index, window.start, window.end)


def person(iid, name, offerings, forbidden=()):
    return Instructor(iid, name, frozenset(offerings), forbidden=tuple(forbidden))


def in_forbidden(forbidden, placement) -> bool:
    """The oracle: an independent restatement of "overlaps a forbidden window"."""
    return any(
        d == placement.day.index and placement.window.start < end and start < placement.window.end
        for d, start, end in forbidden
    )


def solved(snap, **kwargs):
    kwargs.setdefault("time_limit_seconds", 5)
    kwargs.setdefault("workers", 1)
    return solve(snap, **kwargs)


def rule_violations(snap, board):
    return next(r for r in validate(snap, board).results if r.rule_id == RULE_ID).violations


# ── the data: intake ──────────────────────────────────────────────────────


def _plan_row(program, code):
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=code,
        type="mandatory",
        programme_term=1,
        credit_hours=3,
        is_online=False,
        max_capacity=None,
    )


def _intake_world(*, rules):
    """One course, one instructor eligible for it; ``rules`` are InstructorTimeRule kwargs."""
    _plan_row("AI", "AI101")
    for sid in range(9101, 9106):
        Student.objects.create(
            student_id=sid,
            registration_no=str(sid),
            name=f"S{sid}",
            program="AI",
            section="M",
            status="active",
        )
    Room.objects.create(
        room_code="M-1", capacity=40, room_type="lecture", department="AI", section="M"
    )
    teacher = InstructorRow.objects.create(full_name="Dr Ruled", normalised_name="dr ruled")
    CourseInstructor.objects.create(
        program="AI", course_code="AI101", section="M", instructor=teacher
    )
    for row in rules:
        InstructorTimeRule.objects.create(instructor=teacher, **row)
    return teacher


def _build(**kwargs):
    return build_snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=["AI"],
        default_capacity=25,
        **kwargs,
    )


UNAVAILABLE_MORNINGS = {
    "kind": "unavailable",
    "day_mask": 0b00011,  # SUN, MON
    "start_minute": 540,
    "end_minute": 720,
}


@pytest.mark.django_db
def test_intake_loads_an_unavailable_rule_as_forbidden_windows():
    teacher = _intake_world(rules=[UNAVAILABLE_MORNINGS])

    snap = _build(time_rules=True)

    [loaded] = [i for i in snap.instructors if i.id == teacher.id]
    assert loaded.forbidden == ((0, 540, 720), (1, 540, 720))


@pytest.mark.django_db
def test_intake_loads_an_available_only_rule_as_the_complement():
    teacher = _intake_world(
        rules=[
            {"kind": "available_only", "day_mask": 0b00001, "start_minute": 600, "end_minute": 900}
        ]
    )

    [loaded] = [i for i in _build(time_rules=True).instructors if i.id == teacher.id]

    # SUN: everything outside 10:00-15:00; every other day is wholly forbidden
    assert loaded.forbidden[:2] == ((0, 0, 600), (0, 900, 1440))
    assert loaded.forbidden[2:] == ((1, 0, 1440), (2, 0, 1440), (3, 0, 1440), (4, 0, 1440))


@pytest.mark.django_db
def test_intake_merges_several_rules_into_sorted_disjoint_windows():
    teacher = _intake_world(
        rules=[
            {"kind": "unavailable", "day_mask": 0b00001, "start_minute": 540, "end_minute": 660},
            {"kind": "unavailable", "day_mask": 0b00001, "start_minute": 600, "end_minute": 720},
            {"kind": "unavailable", "day_mask": 0b00001, "start_minute": 720, "end_minute": 780},
            {"kind": "unavailable", "day_mask": 0b01000, "start_minute": 780, "end_minute": 840},
        ]
    )

    [loaded] = [i for i in _build(time_rules=True).instructors if i.id == teacher.id]

    # overlapping AND touching windows merge; the Wednesday rule stays its own
    assert loaded.forbidden == ((0, 540, 780), (3, 780, 840))


@pytest.mark.django_db
def test_intake_with_the_switch_off_runs_no_rule_query_and_loads_nothing():
    _intake_world(rules=[UNAVAILABLE_MORNINGS])

    with CaptureQueriesContext(connection) as queries:
        snap = _build(time_rules=False)

    assert not [q for q in queries if "instructor_time_rules" in q["sql"]]
    assert all(not i.forbidden for i in snap.instructors)


@pytest.mark.django_db
def test_intake_with_the_switch_on_reads_the_rules_in_one_query():
    _intake_world(rules=[UNAVAILABLE_MORNINGS])

    with CaptureQueriesContext(connection) as queries:
        _build(time_rules=True)

    assert len([q for q in queries if "instructor_time_rules" in q["sql"]]) == 1


@pytest.mark.django_db
def test_intake_default_follows_the_classic_switch(settings):
    teacher = _intake_world(rules=[UNAVAILABLE_MORNINGS])
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = True

    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    assert not rules_in_force()
    off = _build()
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = True
    assert rules_in_force()
    on = _build()

    assert all(not i.forbidden for i in off.instructors)
    [loaded] = [i for i in on.instructors if i.id == teacher.id]
    assert loaded.forbidden == ((0, 540, 720), (1, 540, 720))


@pytest.mark.django_db
def test_intake_default_needs_one_teacher_per_section_exactly_like_the_classic_engine(settings):
    _intake_world(rules=[UNAVAILABLE_MORNINGS])
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = True
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = False

    assert not rules_in_force()
    assert all(not i.forbidden for i in _build().instructors)


@pytest.mark.django_db
def test_intake_on_with_no_rule_stored_changes_nothing_including_the_fingerprint():
    _intake_world(rules=[])

    with_switch = _build(time_rules=True)
    without = _build(time_rules=False)

    assert all(not i.forbidden for i in with_switch.instructors)
    assert with_switch.fingerprint() == without.fingerprint()


@pytest.mark.django_db
def test_intake_still_refuses_what_it_always_refused():
    with pytest.raises(IntakeError):
        _build(time_rules=True)


def test_the_command_passes_ignore_time_rules_down_to_intake(monkeypatch):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    seen = []

    def fake_build(**kwargs):
        seen.append(kwargs["time_rules"])
        raise IntakeError("stop here")

    monkeypatch.setattr("scheduler.management.commands.sch_plan.build_snapshot", fake_build)
    base = ["--year", "1448", "--term", "1", "--programs", "AI", "--gender", "M"]

    for extra in ([], ["--ignore-time-rules"]):
        with pytest.raises(CommandError):
            call_command("sch_plan", *base, *extra)

    assert seen == [None, False]


# ── the fingerprint ───────────────────────────────────────────────────────


def _tiny(forbidden=()):
    return snapshot(
        [offering()],
        [Section("o1#S1", "o1", 1, 30)],
        [person(5, "Dr A", {"o1"}, forbidden)],
    )


#: The fingerprint of `_tiny()` on master (a73b586), computed with master's own
#: code. A snapshot with no rule must keep it: this is S3 for provenance.
MASTER_TINY_FINGERPRINT = "34233e2fbf4eea5700162f5cf67178b49965cd672cbf6f417c73106e40164340"


def test_a_snapshot_without_rules_keeps_the_fingerprint_it_had_before_rules_existed():
    assert _tiny().fingerprint() == MASTER_TINY_FINGERPRINT


def test_the_fingerprint_changes_with_a_rule_and_with_its_windows():
    plain = _tiny().fingerprint()
    mornings = _tiny(((0, 540, 720),)).fingerprint()
    more = _tiny(((0, 540, 720), (1, 540, 720))).fingerprint()
    other_day = _tiny(((2, 540, 720),)).fingerprint()

    assert len({plain, mornings, more, other_day}) == 4


def test_the_fingerprint_does_not_depend_on_which_instructor_list_order_holds_the_rules():
    a = person(5, "A", {"o1"}, ((0, 540, 720),))
    b = person(6, "B", {"o1"}, ((1, 540, 720),))
    one = snapshot([offering()], [Section("o1#S1", "o1", 1, 30)], [a, b])
    two = snapshot([offering()], [Section("o1#S1", "o1", 1, 30)], [b, a])

    assert one.fingerprint() == two.fingerprint()


# ── the solver: hard, online exempt, nothing else moves ───────────────────


def _two_meeting_section(forbidden, *, instructor=7):
    off = offering(meetings=2)
    return snapshot(
        [off],
        [Section("o1#S1", "o1", 1, 30, instructor_id=instructor)],
        [person(instructor, "Dr A", {"o1"}, forbidden)],
    )


def test_a_meeting_is_never_placed_in_its_instructors_forbidden_time():
    # may teach ONLY Sunday and Tuesday at 13:00: both meetings must land there
    forbidden = allowed_only(cell(SUN, W13), cell(TUE, W13))
    snap = _two_meeting_section(forbidden)

    result = solved(snap)

    assert not result.unplaced
    assert {(p.day, p.window) for p in result.board.placements} == {(SUN, W13), (TUE, W13)}
    assert not any(in_forbidden(forbidden, p) for p in result.board.placements)
    assert not rule_violations(snap, result.board)


def test_an_unavailable_window_is_avoided_but_everything_else_stays_open():
    forbidden = ((0, 540, 720), (1, 540, 720), (2, 540, 720), (3, 540, 720), (4, 540, 720))
    snap = _two_meeting_section(forbidden)

    result = solved(snap)

    assert not result.unplaced
    assert all(p.window == W13 for p in result.board.placements)  # only 13:00 is clear


def test_touching_a_forbidden_window_is_not_overlapping_it():
    # forbidden up to exactly 10:15: the 10:30 slot is clear, and 09:00 is not
    forbidden = tuple((d, 0, 615) for d in range(5))
    snap = _two_meeting_section(forbidden)

    result = solved(snap)

    assert {p.window for p in result.board.placements} <= {W1030, W13}


def test_online_meetings_are_exempt_from_the_rules():
    off = offering(delivery=DeliveryMode.ONLINE, meetings=2)
    forbidden = tuple((d, 0, 1440) for d in range(5))  # the whole week
    snap = snapshot(
        [off],
        [Section("o1#S1", "o1", 1, 30, instructor_id=7)],
        [person(7, "Dr A", {"o1"}, forbidden)],
    )

    result = solved(snap)

    assert not result.unplaced and len(result.board.placements) == 2
    assert not rule_violations(snap, result.board)  # the validator exempts it too


def test_institutional_patterns_exclude_a_bundle_that_touches_forbidden_time():
    policy = get_course_meeting_policy("ENG101")
    off = offering("eng", policy=policy)
    forbidden = tuple((d, 780, 960) for d in range(5))  # no afternoons: only the AM bundle
    snap = snapshot(
        [off],
        [Section("eng#S1", "eng", 1, 25, instructor_id=3)],
        [person(3, "Dr E", {"eng"}, forbidden)],
    )

    result = solved(snap)

    assert not result.unplaced and len(result.board.placements) == 4
    assert {p.window.start for p in result.board.placements} == {540}
    assert policy.accepts(
        [
            {"day": p.day.value, "start_min": p.window.start, "end_min": p.window.end}
            for p in result.board.placements
        ]
    )
    assert not rule_violations(snap, result.board)


def test_a_pattern_section_with_no_legal_bundle_is_reported_unplaced_not_infeasible():
    policy = get_course_meeting_policy("ENG101")
    forbidden = ((0, 600, 610), (0, 800, 810))  # nicks both bundles on Sunday
    snap = snapshot(
        [offering("eng", policy=policy), offering("o2", meetings=1)],
        [
            Section("eng#S1", "eng", 1, 25, instructor_id=3),
            Section("o2#S1", "o2", 1, 25),
        ],
        [person(3, "Dr E", {"eng"}, forbidden)],
    )

    result = solved(snap)

    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert {u.split("#M")[0] for u in result.unplaced} == {"eng#S1"}
    assert [p.section_id for p in result.board.placements] == ["o2#S1"]


def test_a_rule_that_touches_no_cell_leaves_the_solve_exactly_as_it_was():
    """S3: a rule nobody can feel changes neither the board nor the objective."""
    quiet = ((0, 1200, 1300),)  # 20:00-21:40: no declared window comes near it
    base = _two_meeting_section(())
    ruled = _two_meeting_section(quiet)

    a = solved(base, seed=3)
    b = solved(ruled, seed=3)

    assert a.board.placements == b.board.placements
    assert a.objective_value == b.objective_value
    assert a.status == b.status


def test_no_rule_anywhere_means_the_plan_is_what_it_was_and_asks_for_no_second_solve(
    monkeypatch,
):
    import scheduler.solve as solve_module

    calls = []
    real = solve_module.solve

    def spy(snap, **kwargs):
        calls.append(kwargs.get("time_limit_seconds"))
        return real(snap, **kwargs)

    monkeypatch.setattr(solve_module, "solve", spy)
    snap = _two_meeting_section(())

    result = plan(snap, time_limit_seconds=6, workers=1)

    assert result.released_instructors == ()
    assert not result.warnings
    assert len(calls) == 2  # the two passes, nothing extra


# ── the assignment: a ruled instructor never takes what he cannot hold ────


def _single_meeting_courses(count):
    return [offering(f"c{i}") for i in range(count)], [
        Section(f"c{i}#S1", f"c{i}", 1, 30) for i in range(count)
    ]


@pytest.mark.parametrize("open_cells", range(0, 6))
def test_a_ruled_instructor_holds_exactly_as_many_courses_as_he_has_open_cells(open_cells):
    """Oracle: single-meeting courses on distinct cells, so the legal week for n
    sections exists iff he has at least n open cells (nothing else binds)."""
    courses, sections = _single_meeting_courses(5)
    pool = [cell(d, w) for d in (SUN, MON) for w in CELLS_PER_DAY]
    forbidden = (
        allowed_only(*pool[:open_cells]) if open_cells else tuple((d, 0, 1440) for d in range(5))
    )
    snap = snapshot(courses, sections, [person(1, "Dr R", {o.id for o in courses}, forbidden)])

    got = assign_instructors(snap)

    assert sum(1 for s in got.sections if s.instructor_id == 1) == open_cells


def test_a_section_he_cannot_hold_goes_to_the_next_eligible_instructor():
    off = offering(meetings=2)
    # both his open cells are on Sunday: two meetings of one section need two days
    ruled = person(1, "Dr Ruled", {"o1"}, allowed_only(cell(SUN, W9), cell(SUN, W13)))
    free = person(2, "Dr Free", {"o1"})
    snap = snapshot([off], [Section("o1#S1", "o1", 1, 30)], [ruled, free])

    got = assign_instructors(snap)

    assert got.sections[0].instructor_id == 2


def test_a_section_nobody_else_can_take_stays_unassigned_and_still_gets_placed():
    off = offering(meetings=2)
    ruled = person(1, "Dr Ruled", {"o1"}, allowed_only(cell(SUN, W9), cell(SUN, W13)))
    snap = snapshot([off], [Section("o1#S1", "o1", 1, 30)], [ruled])

    got = assign_instructors(snap)
    result = solved(got)

    assert got.sections[0].instructor_id is None
    assert not result.unplaced and len(result.board.placements) == 2


def test_two_sections_and_one_legal_period_assign_only_one():
    off = offering()
    only = person(1, "Dr One", {"o1"}, allowed_only(cell(SUN, W9)))
    snap = snapshot([off], [Section("o1#S1", "o1", 1, 30), Section("o1#S2", "o1", 2, 30)], [only])

    got = assign_instructors(snap)

    assert [s.instructor_id for s in got.sections] == [1, None]


def test_the_probe_applies_the_solvers_own_rules_to_a_pattern_course():
    policy = get_course_meeting_policy("ENG101")
    off = offering("eng", policy=policy)
    morning_only = person(1, "Dr M", {"eng"}, tuple((d, 780, 960) for d in range(5)))
    nicked = person(2, "Dr N", {"eng"}, ((0, 600, 610), (0, 800, 810)))
    section = Section("eng#S1", "eng", 1, 25)
    snap = snapshot([off], [section], [morning_only, nicked])

    assert has_legal_week(snap, morning_only, [section])
    assert not has_legal_week(snap, nicked, [section])
    assert [s.instructor_id for s in assign_instructors(snap).sections] == [1]


def test_a_meeting_the_grid_never_declared_does_not_count_against_the_instructor():
    odd = offering("odd", duration=77)  # no window of 77 minutes exists
    section = Section("odd#S1", "odd", 1, 30)
    teacher = person(1, "Dr R", {"odd"}, ((0, 0, 60),))
    snap = snapshot([odd], [section], [teacher])

    assert has_legal_week(snap, teacher, [section])


def test_the_instructors_own_clash_and_daily_cap_are_part_of_the_week():
    # three courses, one open cell: no clash-free week for two of them
    courses, sections = _single_meeting_courses(2)
    teacher = person(1, "Dr R", {"c0", "c1"}, allowed_only(cell(SUN, W9)))
    snap = snapshot(courses, sections, [teacher])

    assert has_legal_week(snap, teacher, sections[:1])
    assert not has_legal_week(snap, teacher, sections)


def test_the_assignment_never_makes_the_solve_fail_over_random_rules():
    """Property: whatever the rules, assigning and then solving places every
    meeting and breaks no rule. Sections are single-meeting courses with no
    siblings and no rooms, so a legal week per instructor IS a legal board."""
    rng = random.Random(2026)
    pool = [cell(d, w) for d in (SUN, MON, TUE) for w in CELLS_PER_DAY]
    for _ in range(12):
        courses, sections = _single_meeting_courses(6)
        people = []
        for iid in (1, 2):
            opened = rng.sample(pool, rng.randint(0, 5))
            forbidden = allowed_only(*opened) if opened else tuple((d, 0, 1440) for d in range(5))
            eligible = {c.id for c in rng.sample(courses, rng.randint(2, 6))}
            people.append(person(iid, f"Dr {iid}", eligible, forbidden))
        snap = assign_instructors(snapshot(courses, sections, people))

        result = solved(snap)

        assert not result.unplaced, [i.forbidden for i in people]
        assert result.status in {"OPTIMAL", "FEASIBLE"}
        assert not rule_violations(snap, result.board)


# ── the validator ─────────────────────────────────────────────────────────


def _board_snap(forbidden=((0, 540, 720),)):
    snap = snapshot(
        [offering(meetings=2)],
        [Section("o1#S1", "o1", 1, 30, instructor_id=7)],
        [person(7, "Dr A", {"o1"}, forbidden)],
    )
    return snap


def _placement(day, window, *, instructor=7, delivery=DeliveryMode.IN_PERSON, idx=1):
    return Placement(
        section_id="o1#S1",
        offering_id="o1",
        meeting_index=idx,
        kind=MeetingKind.LECTURE,
        delivery=delivery,
        day=day,
        window=window,
        room_id=None,
        instructor_id=instructor,
    )


def test_a_board_that_teaches_in_forbidden_time_is_invalid_with_the_new_rule_id():
    snap = _board_snap()
    bad = Board((_placement(SUN, W9), _placement(MON, W13, idx=2)))
    good = Board((_placement(SUN, W13), _placement(MON, W13, idx=2)))

    report = validate(snap, bad)

    assert report.certification is Certification.INVALID
    assert [v.rule_id for r in report.violated for v in r.violations] == [RULE_ID]
    assert "may not teach" in report.violated[0].violations[0].message
    assert not rule_violations(snap, good)


def test_the_validator_exempts_online_meetings_and_unassigned_sections():
    snap = _board_snap()
    online = Board((_placement(SUN, W9, delivery=DeliveryMode.ONLINE),))
    unassigned = Board((_placement(SUN, W9, instructor=None),))

    assert not rule_violations(snap, online)
    assert not rule_violations(snap, unassigned)


def test_a_meeting_that_only_touches_a_forbidden_window_passes():
    snap = _board_snap(((0, 615, 700),))  # starts exactly where 09:00-10:15 ends
    board = Board((_placement(SUN, W9),))

    assert not rule_violations(snap, board)


def test_the_rule_is_declared_in_the_rulebook_as_a_hard_checked_rule():
    [spec] = [r for r in RULEBOOK if r.rule_id == RULE_ID]
    assert spec.severity.value == "hard"
    assert spec.declaration()["online_exempt"] is True


def test_the_naive_reference_board_honours_the_rules_too():
    """`sch_validate` grades `place_naively`'s board: it must not be flagged by a
    rule it could have respected."""
    forbidden = allowed_only(cell(SUN, W13), cell(TUE, W13))
    snap = _two_meeting_section(forbidden)

    board = place_naively(snap)

    assert len(board.placements) == 2
    assert not any(in_forbidden(forbidden, p) for p in board.placements)
    assert not rule_violations(snap, board)


def test_the_naive_reference_board_places_an_online_section_whatever_the_rules_say():
    snap = snapshot(
        [offering(delivery=DeliveryMode.ONLINE)],
        [Section("o1#S1", "o1", 1, 30, instructor_id=7)],
        [person(7, "Dr A", {"o1"}, tuple((d, 0, 1440) for d in range(5)))],
    )

    assert len(place_naively(snap).placements) == 1


# ── the safety net: a rule never costs a class its place ──────────────────


def _jointly_infeasible():
    """Each instructor can hold his section alone; the two siblings of one
    course can never share the one cell they both need (H10)."""
    only = allowed_only(cell(SUN, W9))
    off = offering()
    return snapshot(
        [off],
        [
            Section("o1#S1", "o1", 1, 30, instructor_id=1),
            Section("o1#S2", "o1", 2, 30, instructor_id=2),
        ],
        [person(1, "Dr One", {"o1"}, only), person(2, "Dr Two", {"o1"}, only)],
    )


def test_the_fixture_really_is_infeasible_with_the_rules_and_fine_without():
    snap = _jointly_infeasible()

    assert solved(snap).status == "INFEASIBLE"
    assert not solved(release_instructors(snap, (1, 2))).unplaced


def test_an_infeasible_rule_set_releases_the_ruled_instructors_and_still_builds():
    snap = _jointly_infeasible()

    result = plan(snap, time_limit_seconds=8, workers=1)

    assert len(result.board.placements) == 2 and not result.unplaced
    assert result.released_instructors == (1, 2)
    [warning] = [w for w in result.warnings if "teaching-time rules" in w]
    assert "Dr One" in warning and "Dr Two" in warning
    # the board was solved against the released snapshot, and validates against it
    released = release_instructors(snap, result.released_instructors)
    assert validate(released, result.board).certification is not Certification.INVALID
    assert all(s.instructor_id is None for s in released.sections)


def test_a_meeting_a_rule_leaves_unplaceable_releases_the_instructor_too():
    # assigned by hand (bypassing the assignment's own check): his rule leaves
    # his only course no cell at all, while the other course places fine
    forbidden = tuple((d, 0, 1440) for d in range(5))
    snap = snapshot(
        [offering("o1"), offering("o2")],
        [Section("o1#S1", "o1", 1, 30, instructor_id=4), Section("o2#S1", "o2", 1, 30)],
        [person(4, "Dr Shut", {"o1"}, forbidden)],
    )
    assert solved(snap).unplaced == ("o1#S1#M1",)

    result = plan(snap, time_limit_seconds=8, workers=1)

    assert not result.unplaced and len(result.board.placements) == 2
    assert result.released_instructors == (4,)
    assert any("Dr Shut" in w for w in result.warnings)


def test_the_release_is_not_taken_when_it_places_nothing_more():
    # a meeting no grid window can hold (77 minutes) is unplaced for EVERYONE, so
    # releasing the ruled instructor gains nothing and must not be done
    snap = snapshot(
        [offering("o1"), offering("odd", duration=77)],
        [Section("o1#S1", "o1", 1, 30, instructor_id=1), Section("odd#S1", "odd", 1, 30)],
        [person(1, "Dr One", {"o1"}, ((0, 1200, 1300),))],
    )
    assert solved(snap).unplaced == ("odd#S1#M1",)

    result = plan(snap, time_limit_seconds=6, workers=1)

    assert result.released_instructors == ()
    assert not any("teaching-time rules" in w for w in result.warnings)
    assert result.unplaced == ("odd#S1#M1",)


def test_a_rule_that_empties_even_the_relaxed_problem_still_releases_the_instructors():
    """plan() drops the same-hour rule when nothing satisfies it; the rules may be
    what leaves the relaxed problem empty too. Here X cannot keep its two lectures
    within one slot of each other (that rule alone empties the first attempt, with
    or without any instructor rule), and the two ruled instructors cannot share
    their one cell (that empties the relaxed attempt)."""
    split = Offering(
        id="x",
        course_code="X",
        course_name="x",
        credit_hours=3,
        programs=frozenset({"AI"}),
        terms=frozenset({1}),
        requirements=(
            MeetingRequirement(
                MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1, allowed_starts=(540,)
            ),
            MeetingRequirement(
                MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1, allowed_starts=(780,)
            ),
        ),
        capacity=30,
        capacity_is_declared=True,
    )
    snap = _jointly_infeasible()
    snap = replace(
        snap,
        offerings=(*snap.offerings, split),
        sections=(*snap.sections, Section("x#S1", "x", 1, 30)),
    )
    free = release_instructors(snap, (1, 2))
    assert solved(free, max_time_of_day_slots=1).status == "INFEASIBLE"  # the same-hour rule
    assert not solved(free).unplaced  # ...and only that rule
    assert solved(snap).status == "INFEASIBLE"  # the instructor rules, on their own

    result = plan(snap, time_limit_seconds=10, workers=1)

    assert len(result.board.placements) == 4 and not result.unplaced
    assert result.released_instructors == (1, 2)
    assert any("the rule was dropped" in w for w in result.warnings)
    assert any("teaching-time rules" in w for w in result.warnings)


def test_the_portfolio_carries_the_release_of_the_run_it_keeps():
    snap = _jointly_infeasible()

    result = plan_portfolio(
        snap, seeds=(1,), time_limit_seconds=8, confirmation_seconds=0, workers=1
    )

    assert result.released_instructors == (1, 2)
    assert len(result.board.placements) == 2


def test_a_run_that_kept_every_assignment_outranks_one_that_released_instructors():
    base = {"days": [1], "unroomed": 0, "astray": 0, "metrics": {"idle_minutes": 10}}
    kept = {**base, "released": 0, "metrics": {"idle_minutes": 500}}
    let_go = {**base, "released": 2}

    assert _portfolio_finalists([let_go, kept], 0.0) == [kept]


def test_restricted_instructors_are_those_with_a_rule_and_a_section():
    snap = snapshot(
        [offering("o1"), offering("o2")],
        [Section("o1#S1", "o1", 1, 30, instructor_id=1), Section("o2#S1", "o2", 1, 30)],
        [
            person(1, "Ruled", {"o1"}, ((0, 0, 60),)),
            person(2, "Ruled but idle", {"o2"}, ((0, 0, 60),)),
            person(3, "Plain", {"o1"}),
        ],
    )

    assert restricted_instructor_ids(snap) == (1,)
    assert release_instructors(snap, ()) is snap


# ── the report ────────────────────────────────────────────────────────────


def test_the_report_names_what_a_rule_cost_an_instructor():
    off = offering(meetings=2)
    ruled = person(1, "Dr Ruled", {"o1"}, allowed_only(cell(SUN, W9), cell(SUN, W13)))
    free = person(2, "Dr Free", {"o1"})
    snap = assign_instructors(snapshot([off], [Section("o1#S1", "o1", 1, 30)], [ruled, free]))

    rows = time_rule_cost(snap)

    assert rows == [{"instructor_id": 1, "name": "Dr Ruled", "sections": ["O1 S1"]}]
    message = describe_time_rule_cost(rows)
    assert "Dr Ruled" in message and "O1 S1" in message


def test_the_report_is_empty_and_runs_nothing_when_nobody_has_a_rule(monkeypatch):
    import scheduler.instructors as instructors_module

    def boom(*args, **kwargs):
        raise AssertionError("no assignment should run for a rule-free snapshot")

    monkeypatch.setattr(instructors_module, "assign_instructors", boom)
    snap = snapshot([offering()], [Section("o1#S1", "o1", 1, 30)], [person(1, "A", {"o1"})])

    assert time_rule_cost(snap) == []
    assert describe_time_rule_cost([]) is None


def test_the_report_is_empty_when_the_rules_cost_nothing():
    off = offering()
    ruled = person(1, "Dr Ruled", {"o1"}, ((0, 1200, 1300),))
    snap = assign_instructors(snapshot([off], [Section("o1#S1", "o1", 1, 30)], [ruled]))

    assert time_rule_cost(snap) == []


def test_readiness_warns_about_an_instructor_whose_rules_leave_no_week_for_a_course():
    shut = person(1, "Dr Shut", {"o1"}, tuple((d, 0, 1440) for d in range(5)))
    fine = person(2, "Dr Fine", {"o1"}, ((0, 1200, 1300),))
    snap = snapshot([offering()], [Section("o1#S1", "o1", 1, 30)], [shut, fine])

    findings = [f for f in assess(snap).findings if f.code == "INSTRUCTOR_TIME_RULES_NO_WEEK"]

    [finding] = findings
    assert finding.severity.value == "warning"
    assert [i["name"] for i in finding.detail["instructors"]] == ["Dr Shut"]
    assert finding.detail["instructors"][0]["courses"] == ["O1"]


def test_readiness_says_nothing_when_every_ruled_instructor_has_a_week():
    fine = person(2, "Dr Fine", {"o1"}, ((0, 1200, 1300),))
    snap = snapshot([offering()], [Section("o1#S1", "o1", 1, 30)], [fine])

    assert not [f for f in assess(snap).findings if f.code == "INSTRUCTOR_TIME_RULES_NO_WEEK"]


# ── the bridge ────────────────────────────────────────────────────────────


def _bridge_world(monkeypatch, settings, make_snapshot):
    """The real bridge over an in-memory snapshot: AI301 ("X COURSE") in term 2, one
    teacher who is eligible for it. ``make_snapshot(teacher_id)`` builds the snapshot."""
    from scheduler import bridge
    from tests.one_teacher_support import FLAG_ON, budget, instructor, link, requirement, scenario

    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    key = requirement("AI", "AI301", "X COURSE")
    sc, _board = scenario()
    teacher = instructor("Dr Ruled")
    link("AI", "AI301", teacher)
    budget(sc, key, 1, code="AI301")
    snap = make_snapshot(teacher.id)
    monkeypatch.setattr(bridge, "build_snapshot", lambda **kwargs: snap)
    return bridge, sc, key, teacher


def _x_course(meetings=2):
    return replace(
        offering("x", meetings=meetings),
        course_code="AI301",
        course_name="X COURSE",
        terms=frozenset({2}),
    )


@pytest.mark.django_db
def test_through_the_bridge_the_stored_teacher_teaches_only_inside_his_allowed_time(
    monkeypatch, settings
):
    from core.models import SectionInstructor, TermSectionMeeting

    forbidden = allowed_only(cell(SUN, W13), cell(TUE, W13))
    bridge, sc, key, teacher = _bridge_world(
        monkeypatch,
        settings,
        lambda tid: snapshot(
            [_x_course()],
            [Section("x#S1", "x", 1, 30)],
            [person(tid, "Dr Ruled", {"x"}, forbidden)],
        ),
    )

    summary = bridge.build_into_scenario(
        sc.id,
        academic_year="1448",
        term=2,
        programs=["AI"],
        gender="M",
        seconds=6,
        runs=1,
        confirmation_seconds=0,
    )

    stored = SectionInstructor.objects.get(scenario=sc, course_key=key, section="S1")
    assert stored.instructor_id == teacher.id
    meetings = list(
        TermSectionMeeting.objects.filter(term_section__scenario=sc).values_list(
            "day", "start_time", "instructor"
        )
    )
    assert sorted((d, st) for d, st, _ in meetings) == [("SUN", "13:00"), ("TUE", "13:00")]
    assert {name for _d, _s, name in meetings} == {"Dr Ruled"}
    assert summary["violations"] == 0
    assert not [w for w in summary["warnings"] if "teaching-time rules" in w]


@pytest.mark.django_db
def test_through_the_bridge_a_rule_that_costs_a_section_is_warned_about_and_the_build_still_succeeds(
    monkeypatch, settings
):
    from core.models import SectionInstructor, TermSectionMeeting

    # his two open cells are both on Sunday, so he cannot hold a two-day section
    forbidden = allowed_only(cell(SUN, W9), cell(SUN, W13))
    bridge, sc, key, teacher = _bridge_world(
        monkeypatch,
        settings,
        lambda tid: snapshot(
            [_x_course()],
            [Section("x#S1", "x", 1, 30)],
            [person(tid, "Dr Ruled", {"x"}, forbidden)],
        ),
    )

    summary = bridge.build_into_scenario(
        sc.id,
        academic_year="1448",
        term=2,
        programs=["AI"],
        gender="M",
        seconds=6,
        runs=1,
        confirmation_seconds=0,
    )

    assert summary["meetings_written"] == 2 and summary["meetings_unplaced"] == 0
    assert any("Dr Ruled" in w and "AI301 S1" in w for w in summary["warnings"])
    assert not SectionInstructor.objects.filter(scenario=sc).exists()
    assert set(
        TermSectionMeeting.objects.filter(term_section__scenario=sc).values_list(
            "instructor", flat=True
        )
    ) == {""}


@pytest.mark.django_db
def test_through_the_bridge_a_release_is_reported_and_nothing_is_credited_to_the_released(
    monkeypatch, settings
):
    from core.models import TermSectionMeeting

    only = allowed_only(cell(SUN, W9))

    def two_rival_sections(tid):
        return snapshot(
            [_x_course(meetings=1)],
            [Section("x#S1", "x", 1, 30), Section("x#S2", "x", 2, 30)],
            [person(tid, "Dr Ruled", {"x"}, only), person(tid + 1000, "Dr Rival", {"x"}, only)],
        )

    bridge, sc, key, teacher = _bridge_world(monkeypatch, settings, two_rival_sections)

    summary = bridge.build_into_scenario(
        sc.id,
        academic_year="1448",
        term=2,
        programs=["AI"],
        gender="M",
        seconds=8,
        runs=1,
        confirmation_seconds=0,
    )

    assert summary["meetings_written"] == 2 and summary["meetings_unplaced"] == 0
    assert any("released" in w and "Dr Ruled" in w for w in summary["warnings"])
    assert summary["meetings_with_an_instructor"] == 0
    assert set(
        TermSectionMeeting.objects.filter(term_section__scenario=sc).values_list(
            "instructor", flat=True
        )
    ) == {""}
