"""Tests for the scheduler rulebook, independent checker and reference placer (S2).

The rules are pure functions over a snapshot and a board, so almost everything
here runs without a database. Each hard rule gets two tests: it must pass a clean
board *and* catch a deliberately broken one. A rule that only ever sees valid
input is indistinguishable from a rule that does nothing.
"""

from __future__ import annotations

from dataclasses import replace

from scheduler.domain import (
    CapacityPolicy,
    DeliveryMode,
    Grid,
    Instructor,
    MeetingKind,
    MeetingRequirement,
    Offering,
    Room,
    Section,
    Snapshot,
    StudentDemand,
    TimeWindow,
)
from scheduler.domain.board import Board, Placement
from scheduler.domain.calendar import Day
from scheduler.placement import place_naively
from scheduler.rules import (
    DECLARED_GAPS,
    RULEBOOK,
    Enforcement,
    Severity,
    rulebook_fingerprint,
)
from scheduler.validate import Certification, validate

GRID = Grid.from_spec(
    lecture_starts={"09:00": 75, "10:30": 75, "13:00": 75},
    lab_starts={"09:00": 100, "10:45": 100},
    online_starts={"15:00": 75, "16:45": 75},
)
W9 = TimeWindow(540, 615)  # 09:00-10:15
W1030 = TimeWindow(630, 705)  # 10:30-11:45
W13 = TimeWindow(780, 855)  # 13:00-14:15
W100 = TimeWindow(540, 640)  # 09:00-10:40


def _offering(oid="off1", *, credits=3, reqs=None, capacity=30, programs=("AI",)):
    return Offering(
        id=oid,
        course_code=oid.upper(),
        course_name=oid,
        credit_hours=credits,
        programs=frozenset(programs),
        terms=frozenset({1}),
        requirements=reqs
        if reqs is not None
        else (MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 2),),
        capacity=capacity,
        capacity_is_declared=True,
    )


def _snapshot(offerings, sections, rooms=(), instructors=(), demand=()):
    return Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=GRID,
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=tuple(rooms),
        instructors=tuple(instructors),
        demand=tuple(demand),
        policy=CapacityPolicy(default_capacity=25),
        source_fingerprint="test",
        created_at="2026-07-26T00:00:00+00:00",
    )


def _p(
    section="off1#S1",
    offering="off1",
    idx=1,
    day=Day.SUN,
    window=W9,
    room=None,
    instructor=None,
    kind=MeetingKind.LECTURE,
    delivery=DeliveryMode.IN_PERSON,
):
    return Placement(
        section_id=section,
        offering_id=offering,
        meeting_index=idx,
        kind=kind,
        delivery=delivery,
        day=day,
        window=window,
        room_id=room,
        instructor_id=instructor,
    )


def _result(report, rule_id):
    return next(r for r in report.results if r.rule_id == rule_id)


# ── rulebook integrity ────────────────────────────────────────────────────


def test_rulebook_fingerprint_is_stable_and_full_length():
    """A fingerprint that decides comparability is not a cache key — full SHA-256."""
    assert len(rulebook_fingerprint()) == 64
    assert rulebook_fingerprint() == rulebook_fingerprint()


def test_every_rule_declaration_is_serialisable_no_lambdas():
    """Declarations must contain no executable selectors, or the rulebook cannot
    be hashed and two runs cannot be proven to have been judged alike."""
    import json

    for spec in RULEBOOK:
        json.dumps(spec.declaration())  # raises if anything is not plain data


def test_rule_ids_are_unique_and_h5_does_not_exist():
    ids = [s.rule_id for s in RULEBOOK] + [g[0] for g in DECLARED_GAPS]
    assert len(ids) == len(set(ids))
    assert "H5" not in ids  # no prayer rule (D2)


def test_declared_gaps_all_carry_a_reason():
    for rule_id, _title, mode, why in DECLARED_GAPS:
        assert mode is not Enforcement.CHECK
        assert why, f"{rule_id} declares a gap with no reason"


# ── each hard rule: passes clean, catches broken ──────────────────────────


def test_h2_catches_two_meetings_on_one_day():
    snap = _snapshot([_offering()], [Section("off1#S1", "off1", 1, 30)])
    good = Board((_p(day=Day.SUN), _p(idx=2, day=Day.MON, window=W1030)))
    bad = Board((_p(day=Day.SUN), _p(idx=2, day=Day.SUN, window=W1030)))
    assert not _result(validate(snap, good), "H2").violations
    assert _result(validate(snap, bad), "H2").violations


def test_h3_catches_a_window_the_grid_never_declared():
    snap = _snapshot([_offering()], [Section("off1#S1", "off1", 1, 30)])
    illegal = TimeWindow(600, 675)  # 10:00-11:15, not a declared start
    bad = Board((_p(window=illegal), _p(idx=2, day=Day.MON, window=W1030)))
    assert _result(validate(snap, bad), "H3").violations


def test_h3_accepts_a_100_minute_lecture_at_a_lab_timing():
    """D6: timing follows duration, room family follows kind."""
    offering = _offering(
        reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 100, 1),)
    )
    snap = _snapshot([offering], [Section("off1#S1", "off1", 1, 30)])
    board = Board((_p(window=W100),))
    assert not _result(validate(snap, board), "H3").violations


def test_h7_catches_an_instructor_double_booked():
    offering = _offering(
        reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    snap = _snapshot(
        [offering, _offering("off2", reqs=offering.requirements)],
        [
            Section("off1#S1", "off1", 1, 30, instructor_id=7),
            Section("off2#S1", "off2", 1, 30, instructor_id=7),
        ],
        instructors=[Instructor(7, "X", frozenset({"off1", "off2"}))],
    )
    clash = Board((_p(instructor=7), _p(section="off2#S1", offering="off2", instructor=7)))
    ok = Board(
        (_p(instructor=7), _p(section="off2#S1", offering="off2", day=Day.MON, instructor=7))
    )
    assert _result(validate(snap, clash), "H7").violations
    assert not _result(validate(snap, ok), "H7").violations


def test_h7_ignores_sections_with_no_instructor():
    """D5: unassigned is a first-class state, not a degraded one."""
    offering = _offering(
        reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    snap = _snapshot(
        [offering, _offering("off2", reqs=offering.requirements)],
        [Section("off1#S1", "off1", 1, 30), Section("off2#S1", "off2", 1, 30)],
    )
    board = Board((_p(), _p(section="off2#S1", offering="off2")))  # same slot, no instructors
    assert not _result(validate(snap, board), "H7").violations


def test_h8_counts_meeting_presences_not_sections():
    offering = _offering(
        reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    offs = [_offering(f"off{i}", reqs=offering.requirements) for i in range(1, 5)]
    secs = [Section(f"off{i}#S1", f"off{i}", 1, 30, instructor_id=7) for i in range(1, 5)]
    snap = _snapshot(offs, secs, instructors=[Instructor(7, "X", frozenset())])
    # four meetings for one instructor on one day, all at different times
    windows = [W9, W1030, W13, TimeWindow(540, 640)]
    board = Board(
        tuple(
            _p(section=f"off{i}#S1", offering=f"off{i}", day=Day.SUN, window=w, instructor=7)
            for i, w in zip(range(1, 5), windows, strict=True)
        )
    )
    assert _result(validate(snap, board), "H8").violations  # cap is 3


def test_h9_catches_a_room_double_booked_but_allows_back_to_back():
    """D8: no turnover — touching meetings in one room are legal."""
    offering = _offering(
        reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    snap = _snapshot(
        [offering, _offering("off2", reqs=offering.requirements)],
        [Section("off1#S1", "off1", 1, 30), Section("off2#S1", "off2", 1, 30)],
        rooms=[Room("R1", "R1", 40, MeetingKind.LECTURE, frozenset({"AI"}))],
    )
    clash = Board((_p(room="R1"), _p(section="off2#S1", offering="off2", room="R1")))
    assert _result(validate(snap, clash), "H9").violations
    # 09:00-10:15 then 10:30-11:45 in the same room — legal, no turnover required
    ok = Board((_p(room="R1"), _p(section="off2#S1", offering="off2", window=W1030, room="R1")))
    assert not _result(validate(snap, ok), "H9").violations


def test_h10_catches_sibling_sections_at_the_same_time():
    offering = _offering(
        reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    snap = _snapshot(
        [offering], [Section("off1#S1", "off1", 1, 30), Section("off1#S2", "off1", 2, 30)]
    )
    clash = Board((_p(section="off1#S1"), _p(section="off1#S2")))
    assert _result(validate(snap, clash), "H10").violations


def test_h11_h12_h14_catch_type_capacity_and_programme():
    offering = _offering(
        capacity=40, reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    section = Section("off1#S1", "off1", 1, 40)
    rooms = [
        Room("LAB", "LAB", 99, MeetingKind.LAB, frozenset({"AI"})),  # wrong type
        Room("TINY", "TINY", 5, MeetingKind.LECTURE, frozenset({"AI"})),  # too small
        Room("OTHER", "OTHER", 99, MeetingKind.LECTURE, frozenset({"CS"})),  # wrong prog
        Room("GOOD", "GOOD", 99, MeetingKind.LECTURE, frozenset({"AI"})),
    ]
    snap = _snapshot([offering], [section], rooms=rooms)
    for bad in ("LAB", "TINY", "OTHER"):
        assert _result(validate(snap, Board((_p(room=bad),))), "H11_H12_H14").violations, bad
    assert not _result(validate(snap, Board((_p(room="GOOD"),))), "H11_H12_H14").violations


def test_h1_catches_a_missing_or_extra_meeting():
    offering = _offering()  # requires 2x75 lecture
    snap = _snapshot([offering], [Section("off1#S1", "off1", 1, 30)])
    too_few = Board((_p(),))
    exact = Board((_p(), _p(idx=2, day=Day.MON)))
    assert _result(validate(snap, too_few), "H1").violations
    assert not _result(validate(snap, exact), "H1").violations


# ── tri-state certification ───────────────────────────────────────────────


def test_a_violation_makes_the_board_invalid():
    snap = _snapshot([_offering()], [Section("off1#S1", "off1", 1, 30)])
    bad = Board((_p(day=Day.SUN), _p(idx=2, day=Day.SUN, window=W1030)))  # H2
    assert validate(snap, bad).certification is Certification.INVALID


def test_a_clean_board_is_uncertified_while_rules_remain_ungraded():
    """'No violation found' and 'not looked for' are different claims. A checker
    that conflates them manufactures confidence."""
    snap = _snapshot([_offering()], [Section("off1#S1", "off1", 1, 30)])
    good = Board((_p(), _p(idx=2, day=Day.MON, window=W1030)))
    report = validate(snap, good)
    assert report.violation_count == 0
    assert report.certification is Certification.UNCERTIFIED
    assert {r.rule_id for r in report.ungraded} >= {"H15", "H16"}


def test_unroomed_meetings_are_observed_not_violations():
    """D7: a room is an assignment that can be left unmade."""
    snap = _snapshot([_offering()], [Section("off1#S1", "off1", 1, 30)])
    board = Board((_p(room=None), _p(idx=2, day=Day.MON, window=W1030, room=None)))
    report = validate(snap, board)
    observed = _result(report, "H_ROOM_REQUIRED")
    assert observed.severity is Severity.OBSERVED
    assert not observed.violations
    assert report.certification is not Certification.INVALID


# ── the reference placer ──────────────────────────────────────────────────


def test_naive_placer_produces_a_board_with_no_hard_violations():
    offerings = [_offering(f"off{i}") for i in range(1, 4)]
    sections = [Section(f"off{i}#S1", f"off{i}", 1, 30) for i in range(1, 4)]
    rooms = [
        Room(f"R{i}", f"R{i}", 40, MeetingKind.LECTURE, frozenset({"AI"})) for i in range(1, 3)
    ]
    snap = _snapshot(offerings, sections, rooms=rooms)
    report = validate(snap, place_naively(snap))
    assert report.violation_count == 0, [r.as_dict() for r in report.violated]


def test_naive_placer_leaves_a_meeting_unroomed_rather_than_dropping_it():
    """With no compatible room at all, the meeting still gets a time (D7)."""
    offering = _offering(
        reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    snap = _snapshot([offering], [Section("off1#S1", "off1", 1, 30)])  # no rooms
    board = place_naively(snap)
    assert len(board.placements) == 1
    assert len(board.unroomed) == 1


def test_naive_placer_is_deterministic():
    offerings = [_offering(f"off{i}") for i in range(1, 5)]
    sections = [Section(f"off{i}#S1", f"off{i}", 1, 30) for i in range(1, 5)]
    rooms = [Room("R1", "R1", 40, MeetingKind.LECTURE, frozenset({"AI"}))]
    snap = _snapshot(offerings, sections, rooms=rooms)
    assert place_naively(snap).placements == place_naively(snap).placements


def test_online_meetings_never_consume_a_room():
    offering = _offering(
        reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.ONLINE, 75, 1),)
    )
    snap = _snapshot(
        [offering],
        [Section("off1#S1", "off1", 1, 30)],
        rooms=[Room("R1", "R1", 40, MeetingKind.LECTURE, frozenset({"AI"}))],
    )
    board = place_naively(snap)
    assert board.physical == ()  # online is not a physical meeting
    assert board.placements[0].room_id is None


# ── instructor assignment and metrics (S4) ────────────────────────────────

from scheduler.instructors import (  # noqa: E402
    DERIVED_WEEKLY_SESSION_CAP,
    MAX_SECTIONS_PER_COURSE,
    assign_instructors,
    instructor_floor_days,
)
from scheduler.solve import instructor_metrics  # noqa: E402


def test_floor_days_is_driven_by_whichever_rule_binds():
    """Two independent rules force the floor: the daily cap, and H2 putting a
    section's meetings on distinct days. Whichever is larger wins."""
    assert instructor_floor_days(9, 2) == 3  # 9/3 = 3 dominates
    assert instructor_floor_days(3, 3) == 3  # a 3-meeting section dominates
    assert instructor_floor_days(0, 0) == 0
    assert instructor_floor_days(15, 2) == 5  # a full week


def _snapshot_with(offerings, sections, instructors):
    return Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=GRID,
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=(),
        instructors=tuple(instructors),
        demand=(),
        policy=CapacityPolicy(default_capacity=25),
        source_fingerprint="t",
        created_at="2026-07-26T00:00:00+00:00",
    )


def test_assignment_caps_sections_of_one_course():
    """D10: at most 2 sections of a course once it runs more than 3; the rest
    are left unlinked rather than forced onto someone."""
    offering = _offering(
        "off1", reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    sections = [Section(f"off1#S{i}", "off1", i, 30) for i in range(1, 6)]  # 5 sections
    snap = _snapshot_with([offering], sections, [Instructor(1, "A", frozenset({"off1"}))])
    out = assign_instructors(snap)
    mine = [s for s in out.sections if s.instructor_id == 1]
    assert len(mine) == MAX_SECTIONS_PER_COURSE
    assert sum(1 for s in out.sections if s.instructor_id is None) == 3  # unlinked (D5)


def test_assignment_respects_the_derived_weekly_cap():
    """3 sessions/day x 5 days = 15. Beyond that, sections go unlinked."""
    offerings, sections = [], []
    for i in range(1, 11):  # 10 courses x 2 sessions = 20 > 15
        o = _offering(
            f"off{i}",
            reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 2),),
        )
        offerings.append(o)
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, 30))
    snap = _snapshot_with(
        offerings, sections, [Instructor(1, "A", frozenset(o.id for o in offerings))]
    )
    out = assign_instructors(snap)
    load = sum(2 for s in out.sections if s.instructor_id == 1)
    assert load <= DERIVED_WEEKLY_SESSION_CAP


def test_a_course_with_few_sections_is_not_capped():
    offering = _offering(
        "off1", reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    sections = [Section(f"off1#S{i}", "off1", i, 30) for i in range(1, 4)]  # 3 only
    snap = _snapshot_with([offering], sections, [Instructor(1, "A", frozenset({"off1"}))])
    out = assign_instructors(snap)
    assert all(s.instructor_id == 1 for s in out.sections)


def test_instructor_metrics_always_carry_their_coverage():
    """D5: partial linkage is permanent, so a bare figure is never published."""
    offering = _offering(
        "off1", reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    snap = _snapshot_with(
        [offering],
        [
            Section("off1#S1", "off1", 1, 30, instructor_id=1),
            Section("off1#S2", "off1", 2, 30),
        ],  # deliberately unassigned
        [Instructor(1, "A", frozenset({"off1"}))],
    )
    board = Board((_p(section="off1#S1", instructor=1), _p(section="off1#S2", day=Day.MON)))
    m = instructor_metrics(snap, board)
    assert m["coverage"]["sections_assigned"] == 1
    assert m["coverage"]["sections_total"] == 2
    assert m["coverage"]["percent"] == 50.0


def test_metrics_report_excess_against_the_proven_floor():
    offering = _offering(
        "off1", reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 2),)
    )
    snap = _snapshot_with(
        [offering],
        [Section("off1#S1", "off1", 1, 30, instructor_id=1)],
        [Instructor(1, "A", frozenset({"off1"}))],
    )
    # two meetings on two days: floor is 2 (H2 forces distinct days), so excess 0
    board = Board(
        (
            _p(section="off1#S1", instructor=1, day=Day.SUN),
            _p(section="off1#S1", idx=2, instructor=1, day=Day.MON),
        )
    )
    m = instructor_metrics(snap, board)
    assert m["working_days"] == 2
    assert m["floor_days"] == 2
    assert m["excess_days"] == 0
    assert m["at_proven_floor"]


# ── the objective's weight currency, the budgets, and the planner (D11) ────
#
# Every test below is behavioural. An earlier draft of this block asserted on
# inspect.getsource() text and on defaults re-derived by the test's own
# arithmetic; review showed those tests mirrored the code rather than
# constraining it, and several would have passed with the term switched off.
# The rule here: change the production behaviour, and one of these must go red.

from scheduler.solve import (  # noqa: E402
    _SCALE,
    _days_by_instructor,
    plan,
    plan_portfolio,
    solve,
    unroomed_count,
)

#: One day, three widely spaced windows — so a two-session day has a unique
#: minimum-gap arrangement and the span term has something to prove.
ONE_DAY = Grid.from_spec(
    lecture_starts={"09:00": 75, "10:30": 75, "13:00": 75},
    lab_starts={"09:00": 100},
    days=(Day.SUN,),
)
#: A single cell: any two meetings placed here MUST collide.
ONE_CELL = Grid.from_spec(
    lecture_starts={"09:00": 75},
    lab_starts={"09:00": 100},
    days=(Day.SUN,),
)
#: Roomy enough that nothing is ever infeasible for want of a window.
ROOMY = Grid.from_spec(
    lecture_starts={"09:00": 75, "10:30": 75, "13:00": 75, "14:30": 75},
    lab_starts={"09:00": 100},
)


def _grid_snapshot(grid, offerings, sections, demand=()):
    return Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=grid,
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=(),
        instructors=(),
        demand=tuple(demand),
        policy=CapacityPolicy(default_capacity=25),
        source_fingerprint="test",
        created_at="2026-07-26T00:00:00+00:00",
    )


def _lecture(n):
    return (MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, n),)


def _one_instructor(grid, n_sections, instructor=1):
    """n separate single-meeting courses, all taught by one person."""
    offerings, sections = [], []
    for i in range(1, n_sections + 1):
        offerings.append(_offering(f"off{i}", reqs=_lecture(1)))
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, 10, instructor_id=instructor))
    return _grid_snapshot(grid, offerings, sections)


# ── span_weight = 0 must switch the term OFF, not floor it to 1 ───────────


def test_the_span_term_shortens_a_teaching_day_when_enabled():
    """Two sessions, three possible windows: 09:00+10:30 is the only pairing
    with a 15-minute gap. If the span term works, the solver must find it."""
    snap = _one_instructor(ONE_DAY, 2)
    result = solve(snap, time_limit_seconds=10, max_working_days=1, span_weight=1 * _SCALE)
    starts = sorted(p.window.start for p in result.board.placements)
    assert starts == [540, 630], f"span term did not minimise the gap: {starts}"


def test_span_weight_zero_removes_the_term_rather_than_flooring_it_to_one():
    """The regression that mattered. `max(1, round(w * ratio))` floored a
    *disabled* weight to 1 point per idle minute, so the planner's first pass —
    which passes span_weight=0 precisely to isolate the working-day question —
    was silently optimising gaps too. With the term genuinely off, the optimum
    is exactly the day term and nothing else."""
    snap = _one_instructor(ONE_DAY, 2)
    off = solve(
        snap,
        time_limit_seconds=10,
        max_working_days=1,
        span_weight=0,
        day_weight=100 * _SCALE,
        alpha=0.9,
    )
    on = solve(
        snap,
        time_limit_seconds=10,
        max_working_days=1,
        span_weight=1 * _SCALE,
        day_weight=100 * _SCALE,
        alpha=0.9,
    )
    day_only = round(100 * _SCALE * ((1 - 0.9) / 0.9))  # one instructor, one day
    assert off.objective_value == day_only, (
        f"span_weight=0 still contributed {off.objective_value - day_only} points"
    )
    assert on.objective_value > day_only  # and enabling it does cost something


def test_day_weight_zero_also_removes_its_term():
    snap = _one_instructor(ONE_DAY, 2)
    r = solve(snap, time_limit_seconds=10, day_weight=0, span_weight=0, alpha=0.9)
    assert r.objective_value == 0.0


# ── the working-day budget ────────────────────────────────────────────────


def test_the_day_budget_is_enforced():
    """Four sessions, cap of 3/day (H8), so two days is the floor. Ample windows
    on five days mean nothing but the budget stops it spreading further."""
    snap = _one_instructor(ROOMY, 4)
    r = solve(snap, time_limit_seconds=15, max_working_days=2, day_weight=0, span_weight=0)
    assert r.board.placements
    assert _days_by_instructor(snap, r.board)[1] <= 2


def test_a_budget_below_the_proven_floor_is_infeasible_not_quietly_widened():
    """Four sessions cannot share one day under H8's 3-per-day cap. Four windows
    per day exist, so a missing window is NOT the reason — the budget is."""
    snap = _one_instructor(ROOMY, 4)
    assert len(ROOMY.day_windows_for(75, DeliveryMode.IN_PERSON)) >= 4
    r = solve(snap, time_limit_seconds=15, max_working_days=1)
    assert not r.board.placements


def test_the_budget_caps_each_instructor_not_just_the_total():
    """A scenario-wide total lets a pass take a day off one person and hand it to
    another; somebody's week gets longer and no check notices.

    Built so the two are NOT equivalent. Instructor 1 has three sessions and
    instructor 2 has two, capped {1: 1, 2: 2} — a total of three. Gap pressure
    makes the solver want to spread, and spreading instructor 1 (three sessions
    crammed into one day) buys far more than spreading instructor 2, so a
    total-only cap of three would spend it on giving instructor 1 a second day.
    The per-instructor cap must forbid exactly that.

    (An earlier version of this test used two sessions each capped at one day
    apiece; the totals then coincided and it passed with the per-instructor
    logic deleted. Mutation testing caught it.)
    """
    offerings, sections = [], []
    for i, instructor in enumerate([1, 1, 1, 2, 2], start=1):
        offerings.append(_offering(f"off{i}", reqs=_lecture(1)))
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, 10, instructor_id=instructor))
    snap = _grid_snapshot(ROOMY, offerings, sections)
    r = solve(
        snap,
        time_limit_seconds=20,
        max_working_days={1: 1, 2: 2},
        day_weight=0,
        span_weight=3 * _SCALE,
        alpha=0.9,
    )
    assert r.board.placements
    per = _days_by_instructor(snap, r.board)
    assert per[1] == 1, f"instructor 1 exceeded their own cap: {per}"
    assert per[2] <= 2


# ── the student-harm ceiling ──────────────────────────────────────────────


def _forced_clash(grid=ONE_CELL):
    """Two courses, 20 shared students, one cell — a clash is unavoidable."""
    a, b = _offering("offa", reqs=_lecture(1)), _offering("offb", reqs=_lecture(1))
    demand = [
        StudentDemand(student_id=i, program="AI", offering_ids=frozenset({"offa", "offb"}))
        for i in range(1, 21)
    ]
    return _grid_snapshot(
        grid,
        [a, b],
        [Section("offa#S1", "offa", 1, 30), Section("offb#S1", "offb", 1, 30)],
        demand=demand,
    )


def test_a_forced_collision_produces_a_positive_clash_score():
    """Without this, every ceiling test below would be vacuous."""
    r = solve(_forced_clash(), time_limit_seconds=10)
    assert r.board.placements
    assert r.clash_score > 0
    assert r.expected_clashes > 0


def test_the_clash_ceiling_binds_hard():
    """The clash is unavoidable, so a ceiling of zero must make the model
    infeasible rather than be quietly ignored."""
    r = solve(_forced_clash(), time_limit_seconds=10, max_clash_score=0)
    assert not r.board.placements


def test_a_zero_tolerance_ceiling_still_admits_the_board_it_came_from():
    """Derived in solver units, not from the recomputed float — the two differ
    wherever a weight was rounded, and the float version could reject its own
    source board."""
    snap = _forced_clash()
    first = solve(snap, time_limit_seconds=10, span_weight=0)
    assert first.clash_score > 0  # the test would prove nothing otherwise
    again = solve(snap, time_limit_seconds=10, max_clash_score=first.clash_score)
    assert again.board.placements, "the ceiling rejected the board that produced it"
    assert again.clash_score <= first.clash_score


# ── plan(), end to end ────────────────────────────────────────────────────


def test_the_planner_never_lengthens_anybody_s_week():
    snap = _one_instructor(ROOMY, 4)
    first = solve(snap, time_limit_seconds=5, span_weight=0)
    planned = plan(snap, time_limit_seconds=12)
    before = _days_by_instructor(snap, first.board)
    after = _days_by_instructor(snap, planned.board)
    assert all(after[i] <= before.get(i, 0) for i in after)


def test_the_planner_runs_one_pass_when_there_is_no_instructor_to_help():
    """The female cohort's case. With nobody assigned there is no day budget to
    protect and no gap to measure, so a second pass would be pure cost."""
    offering = _offering("off1", reqs=_lecture(1))
    snap = _grid_snapshot(ROOMY, [offering], [Section("off1#S1", "off1", 1, 10)])
    result = plan(snap, time_limit_seconds=8)
    assert result.board.placements  # still a real board
    assert not any("two-pass" in n for n in result.notes)


def test_the_planner_survives_an_instructors_only_run():
    """alpha=0 reports clash_score 0 by design. Treating that as a ceiling would
    mean 'no student may ever clash' — instantly infeasible — and the gap pass
    would never run at all."""
    snap = _one_instructor(ROOMY, 3)
    result = plan(snap, time_limit_seconds=12, alpha=0.0)
    assert result.board.placements
    assert any("two-pass" in n for n in result.notes), result.notes


# ── room shortfall: WHY a meeting has no room ─────────────────────────────
#
# The whole point of the module is that "14 unroomed" is unactionable. If the
# buckets blur, the report tells a registrar to reschedule a problem only a
# purchase can fix, or to buy a room when a different hour would have done. So
# each bucket is pinned separately, and the classifier is pinned against the
# placer it explains — a divergence there means the report describes a different
# problem from the one the board actually had.

from scheduler.rooms import (  # noqa: E402
    CONGESTION,
    IMPOSSIBLE,
    SATURATED,
    room_shortfall,
    unroomable_meetings,
)
from scheduler.solve import assign_rooms  # noqa: E402


def _roomed_snapshot(rooms, sections, offerings, grid=ROOMY):
    return Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=grid,
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=tuple(rooms),
        instructors=(),
        demand=(),
        policy=CapacityPolicy(default_capacity=25),
        source_fingerprint="test",
        created_at="2026-07-26T00:00:00+00:00",
    )


def _room(rid, capacity, kind=MeetingKind.LECTURE, programs=("AI",)):
    return Room(id=rid, code=rid, capacity=capacity, kind=kind, programs=frozenset(programs))


def test_a_section_too_big_for_every_room_is_impossible_not_congestion():
    """The live finding this exists for: CS111 needs a lab seating 35 and both
    lab rooms seat 25. No timetable can fix that, and calling it congestion
    would send somebody off to reschedule a purchase decision."""
    offering = _offering("off1", reqs=_lecture(1))
    snap = _roomed_snapshot(
        [_room("SMALL", 25)],
        [Section("off1#S1", "off1", 1, 100)],  # needs far more than 25
        [offering],
    )
    board = assign_rooms(snap, Board((_p(section="off1#S1", offering="off1"),)))
    report = room_shortfall(snap, board)
    assert report["impossible"] == 1
    assert report["congestion"] == 0
    assert report["recoverable"] == 0  # nothing here is worth another solve
    finding = report["findings"][0]
    assert finding["reason"] == IMPOSSIBLE
    assert finding["largest_available"] == 25


def test_a_room_that_exists_but_was_busy_is_congestion():
    """One room, two meetings at the same hour: the second is displaced by this
    board, not by the estate, and a different hour would fit it."""
    offerings = [_offering("offa", reqs=_lecture(1)), _offering("offb", reqs=_lecture(1))]
    snap = _roomed_snapshot(
        [_room("ONLY", 40)],
        [Section("offa#S1", "offa", 1, 10), Section("offb#S1", "offb", 1, 10)],
        offerings,
    )
    board = assign_rooms(
        snap,
        Board(
            (
                _p(section="offa#S1", offering="offa", day=Day.SUN, window=W9),
                _p(section="offb#S1", offering="offb", day=Day.SUN, window=W9),
            )
        ),
    )
    report = room_shortfall(snap, board)
    assert report["congestion"] == 1
    assert report["impossible"] == 0
    finding = report["findings"][0]
    assert finding["reason"] == CONGESTION
    # The claim "they were all busy" is verified against the board, not assumed.
    assert finding["rooms_blocked_at_that_hour"] == 1


def test_an_estate_wide_period_shortage_is_saturated_not_congestion():
    """The subtle one. Every individual meeting looks placeable, so a per-room
    test files each as recoverable congestion — but the week simply does not
    contain enough room-periods, and no rescheduling can conjure one."""
    tiny = Grid.from_spec(
        lecture_starts={"09:00": 75}, lab_starts={"09:00": 100}, days=(Day.SUN, Day.MON)
    )
    offerings, sections, placements = [], [], []
    for i, day in enumerate([Day.SUN, Day.MON, Day.SUN], start=1):
        offerings.append(_offering(f"off{i}", reqs=_lecture(1)))
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, 10))
        placements.append(_p(section=f"off{i}#S1", offering=f"off{i}", day=day, window=W9))
    # 1 room x 2 periods = 2 supply, against 3 meetings needing one.
    snap = _roomed_snapshot([_room("ONLY", 40)], sections, offerings, grid=tiny)
    report = room_shortfall(snap, assign_rooms(snap, Board(tuple(placements))))
    assert report["saturated"] >= 1
    assert report["congestion"] == 0, "an unfixable shortage was sold as reschedulable"
    assert report["recoverable"] == 0
    # One row for the estate, not one per course: the cause is a single
    # shortage, and repeating it per course buries the findings that differ.
    saturated_rows = [f for f in report["findings"] if f["reason"] == SATURATED]
    assert len(saturated_rows) == 1
    finding = saturated_rows[0]
    assert finding["room_periods"] == 2
    assert finding["meetings_needing_them"] == 3
    assert len(finding["courses_affected"]) >= 1


def test_a_room_closed_to_the_programme_is_impossible():
    offering = _offering("off1", reqs=_lecture(1), programs=("DS",))
    snap = _roomed_snapshot(
        [_room("AI-ONLY", 100, programs=("AI",))],
        [Section("off1#S1", "off1", 1, 10)],
        [offering],
    )
    board = assign_rooms(snap, Board((_p(section="off1#S1", offering="off1"),)))
    assert room_shortfall(snap, board)["impossible"] == 1


def test_every_unroomed_meeting_lands_in_exactly_one_bucket():
    """Exhaustive and mutually exclusive, or the totals mislead."""
    offerings = [_offering("offa", reqs=_lecture(1)), _offering("offb", reqs=_lecture(1))]
    snap = _roomed_snapshot(
        [_room("ONLY", 25)],
        [Section("offa#S1", "offa", 1, 10), Section("offb#S1", "offb", 1, 200)],
        offerings,
    )
    board = assign_rooms(
        snap,
        Board(
            (
                _p(section="offa#S1", offering="offa", day=Day.SUN, window=W9),
                _p(section="offb#S1", offering="offb", day=Day.SUN, window=W9),
            )
        ),
    )
    report = room_shortfall(snap, board)
    unroomed = sum(1 for p in board.placements if p.needs_room and p.room_id is None)
    assert report["unroomed"] == unroomed
    assert report["impossible"] + report["saturated"] + report["congestion"] == unroomed


def test_the_classifier_agrees_with_the_placer_it_explains():
    """If the two disagree about what a compatible room is, the report explains a
    different problem from the one the board had."""
    offering = _offering("off1", reqs=_lecture(1))
    snap = _roomed_snapshot(
        [_room("BIG", 100), _room("SMALL", 5)],
        [Section("off1#S1", "off1", 1, 10)],
        [offering],
    )
    board = assign_rooms(snap, Board((_p(section="off1#S1", offering="off1"),)))
    assert board.placements[0].room_id == "BIG"  # SMALL is too small for 10 + buffer
    assert room_shortfall(snap, board)["unroomed"] == 0


def test_unroomable_meetings_is_computable_before_any_board_exists():
    """A floor that depends only on the estate, so a board can be judged against
    what was achievable rather than against zero."""
    offering = _offering("off1", reqs=_lecture(2))
    snap = _roomed_snapshot(
        [_room("SMALL", 10)],
        [Section("off1#S1", "off1", 1, 500)],
        [offering],
    )
    assert unroomable_meetings(snap) == 2  # both weekly meetings, no board needed


# ── exact room assignment ─────────────────────────────────────────────────
#
# Greedy first-fit ("largest section first, smallest sufficient room") is
# genuinely good on the capacity dimension — process the biggest demand first
# and it never wastes a large room. It is *programme* restrictions that break
# it: a room can be large enough and still be closed to the course that needs
# it, and once greedy has spent the only shared room on a class that had
# alternatives, the class with no alternatives has nowhere to go.

from scheduler.rooms import assign_rooms_exact  # noqa: E402


def test_exact_rooming_beats_greedy_when_a_room_is_programme_restricted():
    """The failure greedy cannot see. Two rooms, both big enough; one is open to
    both programmes, the other only to AI. The AI class has a choice, the DS
    class does not. Greedy reaches for the shared room first (it sorts rooms by
    capacity then id) and strands the class that had no alternative."""
    shared = Room(
        id="A-SHARED",
        code="A-SHARED",
        capacity=40,
        kind=MeetingKind.LECTURE,
        programs=frozenset({"AI", "DS"}),
    )
    ai_only = Room(
        id="Z-AI", code="Z-AI", capacity=40, kind=MeetingKind.LECTURE, programs=frozenset({"AI"})
    )
    ai = _offering("offai", reqs=_lecture(1), programs=("AI",))
    ds = _offering("offds", reqs=_lecture(1), programs=("DS",))
    snap = _roomed_snapshot(
        [shared, ai_only],
        [Section("offai#S1", "offai", 1, 30), Section("offds#S1", "offds", 1, 30)],
        [ai, ds],
    )
    board = Board(
        (
            _p(section="offai#S1", offering="offai", day=Day.SUN, window=W9),
            _p(section="offds#S1", offering="offds", day=Day.SUN, window=W9),
        )
    )
    greedy_roomed = sum(1 for p in assign_rooms(snap, board).placements if p.room_id)
    exact = assign_rooms_exact(snap, board, time_limit_seconds=10)
    exact_roomed = sum(1 for p in exact.placements if p.room_id)

    assert greedy_roomed == 1, "fixture no longer reproduces the greedy failure"
    assert exact_roomed == 2, "exact assignment failed to room both"
    # And the DS class must be in the only room open to it.
    ds_place = next(p for p in exact.placements if p.section_id == "offds#S1")
    assert ds_place.room_id == "A-SHARED"


def test_exact_rooming_maximises_the_number_of_rooms_actually_used():
    """Pinned against a known optimum, not against greedy — a test that only
    says "no worse than greedy" is satisfied by returning greedy."""
    offerings = [_offering(f"off{i}", reqs=_lecture(1)) for i in range(1, 4)]
    sections = [Section(f"off{i}#S1", f"off{i}", 1, 10) for i in range(1, 4)]
    snap = _roomed_snapshot([_room("ONLY", 40)], sections, offerings)
    board = Board(
        tuple(
            _p(section=f"off{i}#S1", offering=f"off{i}", day=Day.SUN, window=W9)
            for i in range(1, 4)
        )
    )
    exact = assign_rooms_exact(snap, board, time_limit_seconds=10)
    assert sum(1 for p in exact.placements if p.room_id) == 1


def test_roomed_count_outranks_the_capacity_tie_break():
    """The tie-break must never outvote the thing it breaks ties for.

    Built so the choice is genuinely two-versus-three. One room. Two 100-minute
    classes at 09:00-10:40 and 10:45-12:25 block three 75-minute classes at
    09:00, 10:30 and 12:00 — the three do not overlap each other, so the room can
    host all three, but each of the two long ones straddles two of them.

    With a base of 1000 plus capacity, the two large classes scored 1999 each
    (3998) against three small ones at 1005 each (3015), so the solver rooms TWO
    classes where three could have been roomed — the exact failure the exact pass
    exists to remove. The base must therefore exceed every possible sum of
    tie-breaks, not merely a single one.
    """
    room = Room(
        id="R1", code="R1", capacity=5000, kind=MeetingKind.LECTURE, programs=frozenset({"AI"})
    )
    long_lecture = (MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 100, 1),)
    offerings, sections, places = [], [], []

    # two big classes, each straddling two of the small slots
    for i, window in enumerate([TimeWindow(540, 640), TimeWindow(645, 745)], start=1):
        offerings.append(_offering(f"big{i}", reqs=long_lecture))
        sections.append(Section(f"big{i}#S1", f"big{i}", 1, 999))
        places.append(_p(section=f"big{i}#S1", offering=f"big{i}", day=Day.SUN, window=window))

    # three small classes that do not overlap one another
    for i, window in enumerate(
        [TimeWindow(540, 615), TimeWindow(630, 705), TimeWindow(720, 795)], start=1
    ):
        offerings.append(_offering(f"small{i}", reqs=_lecture(1)))
        sections.append(Section(f"small{i}#S1", f"small{i}", 1, 5))
        places.append(_p(section=f"small{i}#S1", offering=f"small{i}", day=Day.SUN, window=window))

    snap = _roomed_snapshot([room], sections, offerings)
    exact = assign_rooms_exact(snap, Board(tuple(places)), time_limit_seconds=15)
    roomed = sum(1 for p in exact.placements if p.room_id)
    assert roomed == 3, (
        f"roomed {roomed}; the capacity tie-break outvoted the count and took "
        "the two large classes instead of the three small ones"
    )


def test_exact_rooming_never_double_books_a_room():
    """The property that makes the result usable at all.

    Uses windows that OVERLAP WITHOUT SHARING A START. An earlier version
    compared only (room, day, start), which cannot detect that clash — and its
    fixture contained no overlapping windows for it to detect anyway.
    """
    lab = (MeetingRequirement(MeetingKind.LAB, DeliveryMode.IN_PERSON, 100, 1),)
    offerings = [_offering("offa", reqs=lab), _offering("offb", reqs=lab)]
    sections = [Section("offa#S1", "offa", 1, 10), Section("offb#S1", "offb", 1, 10)]
    snap = _roomed_snapshot([_room("R1", 40, kind=MeetingKind.LAB)], sections, offerings)
    board = Board(
        (
            _p(
                section="offa#S1",
                offering="offa",
                day=Day.SUN,
                window=TimeWindow(540, 640),
                kind=MeetingKind.LAB,
            ),  # 09:00-10:40
            _p(
                section="offb#S1",
                offering="offb",
                day=Day.SUN,
                window=TimeWindow(630, 730),
                kind=MeetingKind.LAB,
            ),  # 10:30-12:10
        )
    )
    exact = assign_rooms_exact(snap, board, time_limit_seconds=10)
    roomed = [p for p in exact.placements if p.room_id is not None]
    assert len(roomed) == 1, (
        "both overlapping meetings were given the only room: "
        f"{[(p.room_id, p.window.start, p.window.end) for p in roomed]}"
    )


def test_exact_rooming_allows_back_to_back_in_one_room():
    """D8: no turnover time, so meetings that merely touch may share a room."""
    offerings = [_offering("offa", reqs=_lecture(1)), _offering("offb", reqs=_lecture(1))]
    sections = [Section("offa#S1", "offa", 1, 10), Section("offb#S1", "offb", 1, 10)]
    snap = _roomed_snapshot([_room("R1", 40)], sections, offerings)
    board = Board(
        (
            _p(section="offa#S1", offering="offa", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="offb#S1", offering="offb", day=Day.SUN, window=TimeWindow(615, 690)),
        )
    )
    exact = assign_rooms_exact(snap, board, time_limit_seconds=10)
    assert all(p.room_id == "R1" for p in exact.placements)


def test_exact_rooming_will_not_overfill_a_room():
    """Capacity alone must exclude a room. Split from the programme case below:
    a fixture where BOTH rules exclude every room cannot tell you which one did
    the work, so it tests neither."""
    snap = _roomed_snapshot(
        [_room("SMALL", 5)],
        [Section("off1#S1", "off1", 1, 50)],
        [_offering("off1", reqs=_lecture(1))],  # same programme as the room
    )
    board = Board((_p(section="off1#S1", offering="off1"),))
    exact = assign_rooms_exact(snap, board, time_limit_seconds=10)
    assert exact.placements[0].room_id is None


def test_exact_rooming_will_not_use_a_room_closed_to_the_programme():
    """Programme alone must exclude a room, even one comfortably large enough."""
    snap = _roomed_snapshot(
        [_room("AI-ONLY", 500, programs=("AI",))],
        [Section("off1#S1", "off1", 1, 10)],
        [_offering("off1", reqs=_lecture(1), programs=("DS",))],
    )
    board = Board((_p(section="off1#S1", offering="off1"),))
    exact = assign_rooms_exact(snap, board, time_limit_seconds=10)
    assert exact.placements[0].room_id is None


def test_a_meeting_keeps_one_room_for_its_whole_duration():
    """Rooms are chosen per meeting, not per instant — a class cannot be split
    across two rooms halfway through because the model happened to allow it."""
    long_lab = (MeetingRequirement(MeetingKind.LAB, DeliveryMode.IN_PERSON, 100, 1),)
    snap = _roomed_snapshot(
        [_room("L1", 40, kind=MeetingKind.LAB), _room("L2", 40, kind=MeetingKind.LAB)],
        [Section("off1#S1", "off1", 1, 10), Section("off2#S1", "off2", 1, 10)],
        [_offering("off1", reqs=long_lab), _offering("off2", reqs=long_lab)],
    )
    board = Board(
        (
            _p(section="off1#S1", offering="off1", window=W100, kind=MeetingKind.LAB),
            _p(section="off2#S1", offering="off2", window=W100, kind=MeetingKind.LAB),
        )
    )
    exact = assign_rooms_exact(snap, board, time_limit_seconds=10)
    rooms_used = [p.room_id for p in exact.placements]
    assert len(rooms_used) == 2
    # Both must actually GET a room. An earlier version of this assertion was
    # satisfied by {"L2", None}, i.e. by one class being stranded entirely.
    assert all(r is not None for r in rooms_used), rooms_used
    assert set(rooms_used) == {"L1", "L2"}


# ── room supply is throughput, not declared cells ─────────────────────────
#
# The bug this pins cost a wrong diagnosis, not just a wrong number. Counting
# declared grid cells credited the female cohort with 175 lecture-periods when
# one room can only host 5 a day, so 50 unroomable meetings were reported as
# recoverable congestion — telling a registrar to reschedule a shortage that
# rescheduling cannot touch. Overlapping cells are alternatives, not capacity.

from scheduler.rooms import _saturated_kinds  # noqa: E402


def test_supply_counts_what_a_room_can_host_not_how_many_cells_exist():
    """Two overlapping starts are one opportunity offered twice. A grid with
    three declared lecture cells per day, of which only two can ever be used
    together, must be credited with two."""
    overlapping = Grid.from_spec(
        # 09:00-10:15 and 09:20-10:35 overlap each other; 13:00 is independent.
        lecture_starts={"09:00": 75, "09:20": 75, "13:00": 75},
        lab_starts={"09:00": 100},
        days=(Day.SUN,),
    )
    assert len(overlapping.day_windows_for(75, DeliveryMode.IN_PERSON)) == 3
    # ... but one room can host at most two of them in the day.
    assert overlapping.max_nonoverlapping_per_day(frozenset({75})) == 2

    offerings, sections = [], []
    for i in range(1, 4):  # 3 meetings against a supply of 2
        offerings.append(_offering(f"off{i}", reqs=_lecture(1)))
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, 10))
    snap = _roomed_snapshot([_room("ONLY", 40)], sections, offerings, grid=overlapping)

    saturated = _saturated_kinds(snap)
    assert "LECTURE" in saturated, "a genuine shortage was not detected"
    supply, demand = saturated["LECTURE"]
    assert supply == 2, f"credited {supply} periods; a room can only host 2"
    assert demand == 3


def test_no_shortage_is_reported_when_the_estate_genuinely_suffices():
    """The other direction — over-reporting an unfixable shortage would send
    somebody to buy a room they do not need."""
    roomy = Grid.from_spec(
        lecture_starts={"09:00": 75, "10:30": 75, "13:00": 75},
        lab_starts={"09:00": 100},
        days=(Day.SUN, Day.MON),
    )
    offerings, sections = [], []
    for i in range(1, 4):
        offerings.append(_offering(f"off{i}", reqs=_lecture(1)))
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, 10))
    snap = _roomed_snapshot([_room("ONLY", 40)], sections, offerings, grid=roomy)
    # one room x 2 days x 3 non-overlapping cells = 6 periods for 3 meetings
    assert _saturated_kinds(snap) == {}


# ── the room constraint INSIDE the solver ─────────────────────────────────
#
# Review found this whole block was dead under test: every solve()/plan() test
# built a snapshot with no rooms at all, so the constraint that decides whether a
# board can be roomed was never exercised once. These drive it directly, and each
# reads the objective value, because with students and instructors switched off
# the objective IS the room shortfall and can be predicted exactly.

W1015 = TimeWindow(540, 615)  # 09:00-10:15, the reference 75-minute lecture


def _estate_snapshot(rooms, n_meetings, *, programs=("AI",), grid=ONE_CELL, capacity=10):
    offerings, sections = [], []
    for i in range(1, n_meetings + 1):
        offerings.append(_offering(f"off{i}", reqs=_lecture(1), programs=programs))
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, capacity))
    return Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=grid,
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=tuple(rooms),
        instructors=(),
        demand=(),
        policy=CapacityPolicy(default_capacity=25),
        source_fingerprint="test",
        created_at="2026-07-26T00:00:00+00:00",
    )


def test_the_room_shortfall_is_charged_once_not_once_per_nested_family():
    """Families nest: a meeting that only fits the big room is inside {BIG} and
    inside {BIG,SMALL}. Charging each family separately bills one overflow twice
    and makes the solver prefer boards that strand MORE classes but spread them.
    Hall's deficiency is a MAXIMUM over sets, never a sum.

    Four meetings share one cell. Three need the big room, one fits either.
      {BIG}        : 3 against 1  -> deficiency 2
      {BIG, SMALL} : 4 against 2  -> deficiency 2
    Two meetings genuinely cannot be roomed, so the price must be 2, not 4.
    """
    big = Room(
        id="BIG", code="BIG", capacity=50, kind=MeetingKind.LECTURE, programs=frozenset({"AI"})
    )
    small = Room(
        id="SMALL", code="SMALL", capacity=20, kind=MeetingKind.LECTURE, programs=frozenset({"AI"})
    )
    offerings, sections = [], []
    for i, cap in enumerate([40, 40, 40, 10], start=1):  # 40 needs BIG, 10 fits either
        offerings.append(_offering(f"off{i}", reqs=_lecture(1)))
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, cap))
    snap = Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=ONE_CELL,
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=(big, small),
        instructors=(),
        demand=(),
        policy=CapacityPolicy(default_capacity=25),
        source_fingerprint="t",
        created_at="2026-07-26T00:00:00+00:00",
    )
    price = 7 * _SCALE
    # alpha=1.0 removes the instructor terms and there is no demand, so the
    # objective is exactly the room shortfall.
    r = solve(snap, time_limit_seconds=10, alpha=1.0, unroomed_penalty=price)
    assert r.board.placements
    assert r.objective_value == 2 * price, (
        f"expected a deficiency of 2 priced once ({2 * price}), got {r.objective_value}"
    )


def test_the_shortfall_price_does_not_depend_on_where_the_grid_cuts():
    """A 75-minute lecture costs the same wherever it sits.

    The atoms of a day are cut by every window boundary, including boundaries
    that belong to lab timings and to alternative lecture starts. An identical
    meeting covered one atom at 09:00 and four at 10:30, so a flat per-atom price
    billed the same stranded class 20 or 80 depending on nothing but grid
    geometry. Pricing by atom LENGTH makes the total depend on missing room-time.
    """
    cut_up = Grid.from_spec(
        # A lab ending at 10:40 and another starting at 10:45 slice the 10:30
        # lecture into several atoms; the 09:00 lecture is left whole.
        lecture_starts={"09:00": 75, "10:30": 75},
        lab_starts={"09:00": 100, "10:45": 100},
        days=(Day.SUN,),
    )
    price = 9 * _SCALE
    costs = {}
    for start_hhmm, window in (("09:00", TimeWindow(540, 615)), ("10:30", TimeWindow(630, 705))):
        only_this = Grid.from_spec(
            lecture_starts={start_hhmm: 75},
            lab_starts={"09:00": 100, "10:45": 100},
            days=(Day.SUN,),
        )
        snap = _estate_snapshot([_room("ONLY", 40)], 2, grid=only_this)
        r = solve(snap, time_limit_seconds=10, alpha=1.0, unroomed_penalty=price)
        assert r.board.placements
        assert all(p.window == window for p in r.board.placements)
        costs[start_hhmm] = r.objective_value

    assert costs["09:00"] == costs["10:30"], (
        f"identical shortage priced differently by slot: {costs}"
    )
    assert costs["09:00"] == price  # exactly one meeting too many, one lecture long
    assert cut_up.max_nonoverlapping_per_day(frozenset({75})) == 2  # fixture sanity


def test_hall_condition_binds_on_a_union_of_room_sets():
    """The reason the family set is closed under union.

    L1 serves AI, L3 serves DS, L2 serves both, and L4 serves neither. Two AI
    meetings and two DS meetings at one instant need three rooms between them,
    so one must go unroomed — but no single compatible set shows it: {L1,L2}
    holds its two, {L2,L3} holds its two, and the whole four-room set holds all
    four. Only {L1,L2,L3}, a union, reveals the shortage.
    """
    rooms = [
        Room(id="L1", code="L1", capacity=40, kind=MeetingKind.LECTURE, programs=frozenset({"AI"})),
        Room(
            id="L2",
            code="L2",
            capacity=40,
            kind=MeetingKind.LECTURE,
            programs=frozenset({"AI", "DS"}),
        ),
        Room(id="L3", code="L3", capacity=40, kind=MeetingKind.LECTURE, programs=frozenset({"DS"})),
        Room(id="L4", code="L4", capacity=40, kind=MeetingKind.LECTURE, programs=frozenset({"CS"})),
    ]
    offerings, sections = [], []
    for i, program in enumerate(["AI", "AI", "DS", "DS"], start=1):
        offerings.append(_offering(f"off{i}", reqs=_lecture(1), programs=(program,)))
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, 10))
    snap = Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI", "DS"),
        grid=ONE_CELL,
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=tuple(rooms),
        instructors=(),
        demand=(),
        policy=CapacityPolicy(default_capacity=25),
        source_fingerprint="t",
        created_at="2026-07-26T00:00:00+00:00",
    )
    price = 11 * _SCALE
    r = solve(snap, time_limit_seconds=10, alpha=1.0, unroomed_penalty=price)
    assert r.board.placements
    assert r.objective_value == price, (
        "the union {L1,L2,L3} was not enumerated, so a real shortage was priced "
        f"at {r.objective_value} instead of {price}"
    )
    # And the shortage is genuine: exact assignment can only room three of four.
    roomed = sum(1 for p in assign_rooms_exact(snap, r.board).placements if p.room_id)
    assert roomed == 3


def test_the_solver_spreads_meetings_to_the_rooms_it_actually_has():
    """End to end: given one room and three usable hours, the timing solver must
    not stack three classes into one of them."""
    grid = Grid.from_spec(
        lecture_starts={"09:00": 75, "10:30": 75, "13:00": 75},
        lab_starts={"09:00": 100},
        days=(Day.SUN,),
    )
    snap = _estate_snapshot([_room("ONLY", 40)], 3, grid=grid)
    r = solve(snap, time_limit_seconds=15, alpha=1.0)
    starts = sorted(p.window.start for p in r.board.placements)
    assert len(set(starts)) == 3, f"stacked into the same hour: {starts}"
    assert unroomed_count(snap, r.board) == 0


def test_the_portfolio_never_returns_a_board_worse_roomed_than_its_own_runs():
    """The selection rule ranks rooms above waiting, so the board it keeps must
    strand no more classes than the best run it saw.

    Shipped dark otherwise: review found plan_portfolio had no caller and no
    test, and its comparator would happily take five more unroomed classes to
    save a minute of idle time.
    """
    grid = Grid.from_spec(
        lecture_starts={"09:00": 75, "10:30": 75, "13:00": 75},
        lab_starts={"09:00": 100},
        days=(Day.SUN, Day.MON),
    )
    snap = _estate_snapshot([_room("ONLY", 40)], 4, grid=grid)
    seeds = (1, 2, 3)
    singles = [solve(snap, time_limit_seconds=8, seed=s, alpha=1.0) for s in seeds]
    best_single = min(unroomed_count(snap, r.board) for r in singles)

    chosen = plan_portfolio(snap, seeds=seeds, time_limit_seconds=10, alpha=1.0)
    assert chosen.board.placements
    assert unroomed_count(snap, chosen.board) <= best_single
    assert any("portfolio of" in n for n in chosen.notes)


def test_the_portfolio_reports_the_spread_it_chose_from():
    """A single run of this solver is a lottery ticket — the note has to say so,
    or nobody can tell a lucky board from a reliable one."""
    grid = Grid.from_spec(
        lecture_starts={"09:00": 75, "10:30": 75},
        lab_starts={"09:00": 100},
        days=(Day.SUN,),
    )
    snap = _estate_snapshot([_room("ONLY", 40)], 3, grid=grid)
    chosen = plan_portfolio(snap, seeds=(1, 2), time_limit_seconds=8, alpha=1.0)
    note = next(n for n in chosen.notes if "portfolio of" in n)
    assert "unroomed" in note and "clashes" in note and "spread across runs" in note


def test_the_room_shortfall_budget_binds():
    """The third epsilon-constraint. Without it, the gap pass spends rooms.

    Four classes, one room, one hour: three must go unroomed. A budget of zero
    is therefore impossible to meet and the model must say so rather than
    quietly exceed it.
    """
    snap = _estate_snapshot([_room("ONLY", 40)], 4)
    loose = solve(snap, time_limit_seconds=10, alpha=1.0)
    assert loose.room_shortfall_score > 0, "fixture does not create a shortfall"

    tight = solve(snap, time_limit_seconds=10, alpha=1.0, max_room_shortfall=0)
    assert not tight.board.placements, "a zero room budget was not enforced"


def test_a_room_budget_equal_to_what_was_achieved_is_always_satisfiable():
    """Same guarantee the day and clash budgets give: the board that produced
    the budget still meets it, so a later pass can never be made infeasible by
    being handed its predecessor's own numbers."""
    snap = _estate_snapshot([_room("ONLY", 40)], 4)
    first = solve(snap, time_limit_seconds=10, alpha=1.0)
    again = solve(
        snap, time_limit_seconds=10, alpha=1.0, max_room_shortfall=first.room_shortfall_score
    )
    assert again.board.placements
    assert again.room_shortfall_score <= first.room_shortfall_score


# ── sibling sections back to back ─────────────────────────────────────────
#
# Owner rule, in two parts:
#   (a) instructors are only linked to the department's own courses, so ~73% of
#       sections are service courses whose teacher this system never learns;
#   (b) for those, "if the course has more than 2 sections, try to keep each 2
#       sections back to back -- not all must be back to back; with 3 sections,
#       make 2 back to back and the last can be separated."
#
# So it is a MATCHING: a section pairs with at most one sibling. That
# distinction is the whole point of these tests -- rewarding every adjacent
# combination instead would push four sections into one long consecutive block,
# which is not what was asked and costs the student objective more, since that
# objective wins by spreading siblings apart.

from scheduler.solve import (  # noqa: E402
    _ADJACENT_GAP_MINUTES,
    _max_matching,
    sibling_adjacency,
)

#: Four consecutive teaching slots on one day: 09:00-10:15, 10:30-11:45,
#: 12:00-13:15, 13:30-14:45. Every neighbouring pair has the grid's 15-minute
#: changeover, so the whole day is one adjacency chain.
CHAIN = Grid.from_spec(
    lecture_starts={"09:00": 75, "10:30": 75, "12:00": 75, "13:30": 75},
    lab_starts={"09:00": 100},
    days=(Day.SUN,),
)


def _siblings(grid, n):
    offering = _offering("off1", reqs=_lecture(1))
    return _grid_snapshot(
        grid,
        [offering],
        [Section(f"off1#S{i}", "off1", i, 10) for i in range(1, n + 1)],
    )


def test_two_siblings_are_put_back_to_back_when_rewarded():
    """09:00 + 10:30 are consecutive; 13:00 is an afternoon away."""
    grid = Grid.from_spec(
        lecture_starts={"09:00": 75, "10:30": 75, "13:00": 75},
        lab_starts={"09:00": 100},
        days=(Day.SUN,),
    )
    snap = _siblings(grid, 2)
    r = solve(snap, time_limit_seconds=10, alpha=1.0, sibling_adjacency_weight=5 * _SCALE)
    assert sorted(p.window.start for p in r.board.placements) == [540, 630]
    assert sibling_adjacency(snap, r.board)["percent"] == 100.0


def test_three_siblings_yield_one_pair_and_a_leftover():
    """The owner's own example. Three sections can reach exactly ONE pair, so a
    perfect result is 1/1 -- not 1 out of the three combinations."""
    snap = _siblings(CHAIN, 3)
    r = solve(snap, time_limit_seconds=15, alpha=1.0, sibling_adjacency_weight=5 * _SCALE)
    report = sibling_adjacency(snap, r.board)
    assert report["pairs_achievable"] == 1, "3 sections can only ever make 1 pair"
    assert report["pairs_back_to_back"] == 1
    assert report["percent"] == 100.0


def test_four_siblings_yield_two_disjoint_pairs():
    snap = _siblings(CHAIN, 4)
    r = solve(snap, time_limit_seconds=20, alpha=1.0, sibling_adjacency_weight=5 * _SCALE)
    report = sibling_adjacency(snap, r.board)
    assert report["pairs_achievable"] == 2
    assert report["pairs_back_to_back"] == 2


def test_a_section_is_never_counted_in_two_pairs():
    """Three sections in one consecutive run give TWO adjacencies (1-2 and 2-3)
    but only ONE pair, because the middle section cannot be in both. Counting
    raw adjacencies would score this 2 and overstate the result."""
    snap = _siblings(CHAIN, 3)
    board = Board(
        tuple(
            _p(section=f"off1#S{i}", offering="off1", day=Day.SUN, window=w)
            for i, w in enumerate(
                [TimeWindow(540, 615), TimeWindow(630, 705), TimeWindow(720, 795)], start=1
            )
        )
    )
    report = sibling_adjacency(snap, board)
    assert report["pairs_back_to_back"] == 1, "the middle section was double-counted"


def test_max_matching_picks_disjoint_pairs():
    assert _max_matching([]) == 0
    assert _max_matching([("a", "b")]) == 1
    assert _max_matching([("a", "b"), ("b", "c")]) == 1  # share b
    assert _max_matching([("a", "b"), ("c", "d")]) == 2  # disjoint
    # Greedy from the left would take (b,c) and then stall at 1; the maximum is 2.
    assert _max_matching([("b", "c"), ("a", "b"), ("c", "d")]) == 2


def test_a_long_wait_does_not_count_as_back_to_back():
    """Only the NEXT teaching slot counts. 11:45 to 13:00 is 75 minutes -- a
    break, not a changeover, and rewarding it would make the measure useless."""
    grid = Grid.from_spec(
        lecture_starts={"09:00": 75, "13:00": 75},
        lab_starts={"09:00": 100},
        days=(Day.SUN,),
    )
    snap = _siblings(grid, 2)
    r = solve(snap, time_limit_seconds=10, alpha=1.0, sibling_adjacency_weight=5 * _SCALE)
    assert sibling_adjacency(snap, r.board)["percent"] == 0.0


def test_adjacency_is_never_credited_across_different_days():
    snap = _siblings(ROOMY, 2)
    board = Board(
        (
            _p(section="off1#S1", offering="off1", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="off1#S2", offering="off1", day=Day.MON, window=TimeWindow(630, 705)),
        )
    )
    assert sibling_adjacency(snap, board)["percent"] == 0.0


def test_the_reward_is_off_by_default():
    """It opposes the student objective, so it may not switch itself on."""
    import inspect

    assert inspect.signature(solve).parameters["sibling_adjacency_weight"].default == 0


def test_the_changeover_threshold_matches_the_real_grid():
    """15 minutes is the live grid's changeover (09:00-10:15 then 10:30); the
    next real gap is 55 minutes. The threshold has to sit between them."""
    assert 15 <= _ADJACENT_GAP_MINUTES < 55


def test_the_model_rewards_at_most_one_pair_per_section():
    """Pins the matching rule INSIDE the objective, not just in the report.

    Three sections in one consecutive run contain two adjacencies (1-2 and 2-3).
    Without the at-most-one-pair constraint the solver collects both and is paid
    twice for a single leftover-free block — so it will chain sections together
    wherever it can, which is exactly what the owner said not to do. With it,
    three sections are worth exactly one pair.

    Checked on objective_value because the matching METRIC cannot see the
    difference: it scores a 3-chain as one pair either way, so a metric-based
    test passes with the constraint deleted (mutation testing showed precisely
    that).
    """
    weight = 5 * _SCALE
    snap = _siblings(CHAIN, 3)
    r = solve(snap, time_limit_seconds=15, alpha=1.0, sibling_adjacency_weight=weight)
    assert r.board.placements
    assert r.objective_value == -weight, (
        f"expected exactly one rewarded pair ({-weight}), got {r.objective_value} "
        "— a section is being counted in two pairs"
    )


def test_four_sections_are_worth_exactly_two_pairs_in_the_objective():
    weight = 5 * _SCALE
    snap = _siblings(CHAIN, 4)
    r = solve(snap, time_limit_seconds=20, alpha=1.0, sibling_adjacency_weight=weight)
    assert r.board.placements
    assert r.objective_value == -2 * weight, r.objective_value


def test_the_back_to_back_reward_cannot_cost_a_working_day():
    """Pass 1 answers one question — how few days can these instructors work.

    Measured on the live cohort, letting the back-to-back reward act in pass 1
    pushed the day count off its proven floor from weight 3 upward (19 -> 20 ->
    21). Worse, pass 2's day budget is DERIVED from pass 1, so the loss was
    locked in and no later pass could recover it. The reward therefore belongs
    only to pass 2, where days are a hard budget and cannot be spent.
    """
    import inspect

    source = inspect.getsource(plan)
    first_pass = source[source.index("first = solve(") : source.index("per_instructor =")]
    assert "sibling_adjacency_weight=0" in first_pass, (
        "pass 1 must pin the back-to-back reward off, or it can spend a "
        "working day and fix the loss into pass 2's budget"
    )


def test_the_planner_holds_the_day_floor_with_the_reward_on():
    """The behavioural half of the guarantee above."""
    offerings, sections = [], []
    for i in range(1, 4):  # one course, three sections, one instructor
        offerings.append(_offering(f"off{i}", reqs=_lecture(1)))
        sections.append(Section(f"off{i}#S1", f"off{i}", 1, 10, instructor_id=1))
    snap = _grid_snapshot(ROOMY, offerings, sections)

    without = plan(snap, time_limit_seconds=12)
    with_reward = plan(snap, time_limit_seconds=12, sibling_adjacency_weight=30 * _SCALE)
    before = _days_by_instructor(snap, without.board)
    after = _days_by_instructor(snap, with_reward.board)
    assert all(after[i] <= before.get(i, 0) for i in after), (
        f"the reward bought extra working days: {before} -> {after}"
    )


def test_the_planner_switches_the_pairing_on_but_the_raw_solver_does_not():
    """Policy belongs to the planner, not the primitive.

    `solve()` is the neutral engine — a caller asking it for a board should not
    silently get an objective they did not request. `plan()` is where the
    owner's rules live, and the pairing is one of them, measured close to free
    at its default weight.
    """
    import inspect

    assert inspect.signature(solve).parameters["sibling_adjacency_weight"].default == 0
    planner_default = inspect.signature(plan).parameters["sibling_adjacency_weight"].default
    assert planner_default > 0, "the owner asked for this; it should be on"
    assert planner_default == 3 * _SCALE, "weight changed without re-measuring the trade"


# ── seating real students (the confirmation the objective never faced) ────
#
# The planner optimises a PROXY: expected clashes = sum shared/(na*nb), which
# assumes students land in sections at random. These tests pin the confirmation
# that turns that into a statement about actual students — including the part
# the proxy cannot see, which is that sections have capacity.

from scheduler.seating import SeatingResult, seat_students  # noqa: E402


def _two_course_snapshot(grid, *, capacity=30, students=10, sections_each=1):
    offerings, sections = [], []
    for code in ("offa", "offb"):
        offerings.append(_offering(code, reqs=_lecture(1)))
        for i in range(1, sections_each + 1):
            sections.append(Section(f"{code}#S{i}", code, i, capacity))
    demand = [
        StudentDemand(student_id=n, program="AI", offering_ids=frozenset({"offa", "offb"}))
        for n in range(1, students + 1)
    ]
    return _grid_snapshot(grid, offerings, sections, demand=demand)


def test_a_genuine_collision_is_reported_as_a_real_clash():
    """One cell, two courses everyone needs: nobody can escape it."""
    snap = _two_course_snapshot(ONE_CELL)
    board = Board(
        (
            _p(section="offa#S1", offering="offa", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="offb#S1", offering="offb", day=Day.SUN, window=TimeWindow(540, 615)),
        )
    )
    result = seat_students(snap, board, time_limit_seconds=20)
    assert result.students == 10
    assert result.students_with_a_clash == 10
    assert result.clash_free_percent == 0.0
    assert result.unseated_demands == 0


def test_a_separated_board_leaves_every_student_clash_free():
    snap = _two_course_snapshot(ROOMY)
    board = Board(
        (
            _p(section="offa#S1", offering="offa", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="offb#S1", offering="offb", day=Day.SUN, window=TimeWindow(630, 705)),
        )
    )
    result = seat_students(snap, board, time_limit_seconds=20)
    assert result.students_with_a_clash == 0
    assert result.clash_free_percent == 100.0


def test_seating_respects_section_capacity():
    """The thing the proxy structurally cannot see.

    Two sections of each course, but each holds only 5 of the 10 students, so
    the seating cannot simply put everyone in the clash-free one. This is why
    seating is solved rather than counted: capacity is what makes it an
    assignment problem.
    """
    snap = _two_course_snapshot(ROOMY, capacity=5, students=10, sections_each=2)
    board = Board(
        (
            # offa S1 collides with offb S1; the S2 pair is clear.
            _p(section="offa#S1", offering="offa", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="offb#S1", offering="offb", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="offa#S2", offering="offa", day=Day.MON, window=TimeWindow(540, 615)),
            _p(section="offb#S2", offering="offb", day=Day.MON, window=TimeWindow(630, 705)),
        )
    )
    result = seat_students(snap, board, time_limit_seconds=20)
    assert result.unseated_demands == 0, "capacity was sufficient; nobody should be turned away"
    # Only 5 seats in the clash-free offa#S2, so at least 5 students must take
    # offa#S1 -- and every one of those collides with whichever offb they get
    # only if they also take offb#S1. The optimum puts those 5 into offb#S2.
    assert result.students_with_a_clash == 0


def test_students_who_cannot_be_seated_are_reported_not_hidden():
    """Ten students, five seats. The five without a place must be visible."""
    snap = _two_course_snapshot(ROOMY, capacity=5, students=10)
    board = Board(
        (
            _p(section="offa#S1", offering="offa", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="offb#S1", offering="offb", day=Day.MON, window=TimeWindow(540, 615)),
        )
    )
    result = seat_students(snap, board, time_limit_seconds=20)
    assert result.unseated_demands == 10, (
        f"expected 10 unmet demands (2 courses x 5 students over capacity), "
        f"got {result.unseated_demands}"
    )


def test_two_online_sessions_at_the_same_hour_do_clash_for_a_student():
    """D9 as revised, 2026-07-28. Online used to be exempt from student conflict,
    licensed by its private late-day family: a session after the campus day
    competes with nothing.

    That family is gone -- it was the only thing this engine scheduled at hours
    the scenario's grid does not declare, and drawing them means widening a grid
    the EXISTING engine treats as legal placement times. Online now runs at the
    declared hours, so the exemption would be a lie: a class at 13:00 occupies a
    student's 13:00 whether they attend it in a room or at home."""
    offerings = [
        _offering(
            code, reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.ONLINE, 100, 1),)
        )
        for code in ("offa", "offb")
    ]
    sections = [Section("offa#S1", "offa", 1, 30), Section("offb#S1", "offb", 1, 30)]
    demand = [
        StudentDemand(student_id=n, program="AI", offering_ids=frozenset({"offa", "offb"}))
        for n in range(1, 6)
    ]
    snap = _grid_snapshot(ROOMY, offerings, sections, demand=demand)
    board = Board(
        (
            _p(
                section="offa#S1",
                offering="offa",
                day=Day.SUN,
                window=TimeWindow(900, 1000),
                delivery=DeliveryMode.ONLINE,
            ),
            _p(
                section="offb#S1",
                offering="offb",
                day=Day.SUN,
                window=TimeWindow(900, 1000),
                delivery=DeliveryMode.ONLINE,
            ),
        )
    )
    result = seat_students(snap, board, time_limit_seconds=20)
    assert result.students_with_a_clash == 5, (
        "two classes at the same hour collide for every student who needs both, "
        "and being online does not give anyone a second afternoon"
    )


def test_an_online_session_still_consumes_no_room():
    """The half of D9 that survives, and the reason it is a delivery mode rather
    than a time of day: no room is needed at any hour."""
    board = Board(
        (
            _p(
                section="offa#S1",
                offering="offa",
                day=Day.SUN,
                window=TimeWindow(540, 640),
                delivery=DeliveryMode.ONLINE,
            ),
        )
    )
    assert board.unroomed == (), "an online session was counted as needing a room"
    assert not board.placements[0].needs_room


def test_student_waiting_time_is_measured():
    """The figure that turned out to matter: killing clashes lengthens student
    days, and nothing in the planner was watching it."""
    snap = _two_course_snapshot(ROOMY, students=1)
    board = Board(
        (
            _p(section="offa#S1", offering="offa", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="offb#S1", offering="offb", day=Day.SUN, window=TimeWindow(780, 855)),
        )
    )
    result = seat_students(snap, board, time_limit_seconds=20)
    # 09:00-10:15 then 13:00-14:15: a span of 315 minutes containing 150 of
    # teaching, so 165 minutes of waiting.
    assert result.idle_minutes == 165


def test_the_student_gap_term_is_off_by_default_in_both_layers():
    """It helps students by hurting instructors, and the instructor timetable is
    the stated priority.

    Measured by seating real students: weight 1 saves students ~19% of their
    waiting and costs instructors ~21% of theirs. They pull on the same lever in
    opposite directions — packing a student's day spreads an instructor's. That
    is a trade for the owner to opt into, not a default.
    """
    import inspect

    assert inspect.signature(solve).parameters["student_adjacency_weight"].default == 0
    assert inspect.signature(plan).parameters["student_adjacency_weight"].default == 0


def test_the_student_gap_term_pulls_co_demanded_courses_together():
    """Two courses everyone takes, three slots: 09:00 and 10:30 are consecutive,
    13:00 is an afternoon away. With the term on, the pair must be adjacent."""
    grid = Grid.from_spec(
        lecture_starts={"09:00": 75, "10:30": 75, "13:00": 75},
        lab_starts={"09:00": 100},
        days=(Day.SUN,),
    )
    offerings = [_offering("offa", reqs=_lecture(1)), _offering("offb", reqs=_lecture(1))]
    sections = [Section("offa#S1", "offa", 1, 30), Section("offb#S1", "offb", 1, 30)]
    demand = [
        StudentDemand(student_id=n, program="AI", offering_ids=frozenset({"offa", "offb"}))
        for n in range(1, 21)  # well above the shared-student threshold
    ]
    snap = _grid_snapshot(grid, offerings, sections, demand=demand)
    r = solve(snap, time_limit_seconds=15, alpha=1.0, student_adjacency_weight=5 * _SCALE)
    starts = sorted(p.window.start for p in r.board.placements)
    assert starts == [540, 630], f"co-demanded courses left far apart: {starts}"


def test_a_thinly_shared_pair_is_not_worth_a_variable():
    """The term is quadratic in sections, so it is spent on the pairs that
    actually shape a student's week — not on one shared by two people."""
    from scheduler.solve import _MIN_SHARED_FOR_GAP_TERM

    assert _MIN_SHARED_FOR_GAP_TERM >= 2


# ── every placement must have a column to be drawn in ─────────────────────
#
# Both the workbook export and the on-screen grid build their columns from the
# scenario's slot config and match a placement by its exact start time. A start
# with no column is simply not drawn — no error, no warning, just missing
# classes, which is the worst way for data to be wrong.
#
# It happened: online classes sit in their own late family (15:00, 16:45, 18:30)
# that the original engine has no notion of, so 30 of 644 placements had nowhere
# to go and the export dropped them silently.

from scheduler.bridge import LAB_DURATION_MINUTES, grid_columns_for  # noqa: E402


def _board_at(*windows):
    return Board(
        tuple(
            _p(section=f"off{i}#S1", offering=f"off{i}", day=Day.SUN, window=w)
            for i, w in enumerate(windows, start=1)
        )
    )


def test_a_time_the_existing_grid_never_heard_of_still_gets_a_column():
    """The actual failure: an 18:30 online session against a grid that stops at
    16:00."""
    existing = [{"label": "09:00-10:15", "start": "09:00", "end": "10:15"}]
    board = _board_at(TimeWindow(540, 615), TimeWindow(1110, 1210))  # 09:00, 18:30
    lecture, lab, added_lecture, added_lab = grid_columns_for(board, existing, [])

    starts = {row["start"] for row in lecture} | {row["start"] for row in lab}
    assert "18:30" in starts, "a placement was left with no column to be drawn in"
    assert added_lab == 1


def test_every_placement_on_the_board_ends_up_with_a_column():
    """The property that matters, stated directly."""
    board = _board_at(
        TimeWindow(540, 615),  # 09:00 lecture
        TimeWindow(630, 705),  # 10:30 lecture
        TimeWindow(645, 745),  # 10:45 lab
        TimeWindow(900, 1000),  # 15:00 online
        TimeWindow(1110, 1210),  # 18:30 online
    )
    lecture, lab, _, _ = grid_columns_for(board, [], [])
    covered = {row["start"] for row in lecture} | {row["start"] for row in lab}
    for placement in board.placements:
        start = f"{placement.window.start // 60:02d}:{placement.window.start % 60:02d}"
        assert start in covered, f"{start} has no column"


def test_existing_columns_are_never_removed():
    """Only ever adds. A scenario must not lose a column something else relies
    on just because this engine did not happen to use it."""
    existing_lecture = [
        {"label": "09:00-10:15", "start": "09:00", "end": "10:15"},
        {"label": "13:00-14:15", "start": "13:00", "end": "14:15"},
    ]
    existing_lab = [{"label": "Lab 1", "start": "09:00", "end": "10:40"}]
    board = _board_at(TimeWindow(540, 615))  # uses only the 09:00 lecture

    lecture, lab, added_lecture, added_lab = grid_columns_for(board, existing_lecture, existing_lab)
    assert added_lecture == 0 and added_lab == 0
    assert {row["start"] for row in lecture} == {"09:00", "13:00"}
    assert {row["start"] for row in lab} == {"09:00"}
    # labels of pre-existing columns survive untouched
    assert any(row["label"] == "Lab 1" for row in lab)


def test_a_column_is_not_added_twice():
    existing = [{"label": "09:00-10:15", "start": "09:00", "end": "10:15"}]
    board = _board_at(TimeWindow(540, 615), TimeWindow(540, 615))
    lecture, _lab, added, _ = grid_columns_for(board, existing, [])
    assert added == 0
    assert len(lecture) == 1


def test_long_meetings_go_to_the_lab_columns_and_short_ones_to_lectures():
    """Both surfaces split by duration, so the seam must split the same way or a
    meeting is filed under a table that will never look for it."""
    board = _board_at(TimeWindow(540, 615), TimeWindow(540, 640))  # 75 min, 100 min
    lecture, lab, _, _ = grid_columns_for(board, [], [])
    assert {row["start"] for row in lecture} == {"09:00"}
    assert {row["start"] for row in lab} == {"09:00"}
    assert lecture[0]["end"] == "10:15"
    assert lab[0]["end"] == "10:40"
    assert 75 <= LAB_DURATION_MINUTES < 100


# ── one board per course ──────────────────────────────────────────────────
#
# Boards are how the workbook and the screen SEGMENT the timetable, and both
# render one cell per placement row. A section written to every board whose
# students needed it was therefore drawn once per board in the SAME cell — five
# copies of "DS321 S2" in one square — and every course read "(on T1)" because
# it genuinely sat on Term 1 as well as its own. The export was unreadable.
#
# The section still has one schedule; that was never the part boards duplicated.

from scheduler.bridge import choose_board  # noqa: E402


def _offering_in_terms(*terms, code="AI101", name="INTRO"):
    return Offering(
        id=f"off:{code}",
        course_code=code,
        course_name=name,
        credit_hours=3,
        programs=frozenset({"AI"}),
        terms=frozenset(terms),
        requirements=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),),
        capacity=25,
        capacity_is_declared=True,
    )


def test_the_budget_plan_term_decides_where_a_course_goes():
    """The scenario's own answer, and where a registrar looks for it."""
    offering = _offering_in_terms(1, 3)
    board = choose_board(
        offering,
        plan_term_of_key={"AI101::INTRO": 3},
        board_of_term={1: 111, 3: 333},
        headcount={111: 500},  # far more students on board 111 — must not win
    )
    assert board == 333


def test_the_plan_is_used_when_the_budget_has_no_term():
    """The real case: CHEM101's budget row carries a null plan term, and guessing
    from headcount put a first-term course on the Term 7 board."""
    offering = _offering_in_terms(1, code="CHEM101", name="CHEMISTRY")
    board = choose_board(
        offering,
        plan_term_of_key={},  # budget says nothing
        board_of_term={1: 111, 7: 777},
        headcount={777: 40, 111: 2},  # headcount alone would say Term 7
    )
    assert board == 111, "a Term 1 course was filed under Term 7"


def test_headcount_is_the_last_resort_not_the_first():
    offering = _offering_in_terms()  # no plan terms at all
    board = choose_board(
        offering,
        plan_term_of_key={},
        board_of_term={1: 111, 3: 333},
        headcount={333: 9, 111: 4},
    )
    assert board == 333


def test_the_earliest_plan_term_wins_when_a_course_spans_several():
    """A course named in several plan terms is met first at the earliest one."""
    offering = _offering_in_terms(5, 1, 3)
    board = choose_board(
        offering,
        plan_term_of_key={},
        board_of_term={1: 111, 3: 333, 5: 555},
    )
    assert board == 111


def test_a_course_with_nowhere_to_go_is_reported_not_guessed():
    offering = _offering_in_terms(9)
    assert (
        choose_board(offering, plan_term_of_key={}, board_of_term={1: 111}, headcount=None) is None
    )


def test_the_choice_is_a_single_board_not_a_set():
    """The whole point: one board, so one cell, so one copy."""
    offering = _offering_in_terms(1, 3, 5)
    board = choose_board(
        offering,
        plan_term_of_key={"AI101::INTRO": 1},
        board_of_term={1: 111, 3: 333, 5: 555},
        headcount={333: 7, 555: 7},
    )
    assert isinstance(board, int)


# ── a section keeps the same hour all week ────────────────────────────────
#
# Owner rule: "for a section, let's say the first lecture was 9am — the next
# lecture for that section is not good after noon, or late like after 15:00.
# Preferably keep it at the same time slot; if not, one slot before or after,
# not too far."
#
# Measured on the live male cohort before this existed: 14% of sections kept
# their slot, the average wandered 156 minutes, and the worst wandered SEVEN
# HOURS. Nothing in the model had an opinion — the clash term only cares what
# sits on top of a meeting, and the instructor terms only care which days are
# used — so scattering was free, and scattering is how the clash term wins.

from scheduler.domain.calendar import Slot  # noqa: E402
from scheduler.solve import time_of_day_drift  # noqa: E402

#: 09:00 on Sunday, 10:30 on Monday, 13:00 on Tuesday -- one lecture time per
#: day, so a section's two weekly lectures CANNOT share a start and the amount
#: of drift is decided entirely by which days it picks. Monday also carries the
#: only lab slot, which is what gives the solver a reason to avoid Monday.
_SUN9 = TimeWindow(540, 615)
_MON1030 = TimeWindow(630, 705)
_TUE13 = TimeWindow(780, 855)
_MON_LAB = TimeWindow(645, 745)  # 10:45-12:25, overlaps the Monday lecture

DRIFT_GRID = Grid(
    slots=(
        Slot(Day.SUN, _SUN9, MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
        Slot(Day.MON, _MON1030, MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
        Slot(Day.TUE, _TUE13, MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
        Slot(Day.MON, _MON_LAB, MeetingKind.LAB, DeliveryMode.IN_PERSON),
    )
)


def _drift_snapshot():
    """One lecture course against one Monday-only lab that its students share.

    Left alone the solver puts the lectures on Sunday and Tuesday: that dodges
    a hundred-student clash with the lab and costs nothing it can see. It is
    also 09:00 one day and 13:00 the next — exactly the complaint.
    """
    lectures = Offering(
        id="lec",
        course_code="LEC",
        course_name="lecture course",
        credit_hours=3,
        programs=frozenset({"AI"}),
        terms=frozenset({1}),
        requirements=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 2),),
        capacity=200,
        capacity_is_declared=True,
    )
    lab = Offering(
        id="lab",
        course_code="LAB",
        course_name="lab course",
        credit_hours=1,
        programs=frozenset({"AI"}),
        terms=frozenset({1}),
        requirements=(MeetingRequirement(MeetingKind.LAB, DeliveryMode.IN_PERSON, 100, 1),),
        capacity=200,
        capacity_is_declared=True,
    )
    both = frozenset({"lec", "lab"})
    snapshot = _snapshot(
        [lectures, lab],
        [Section("lec#S1", "lec", 1, 200), Section("lab#S1", "lab", 1, 200)],
        demand=[StudentDemand(f"s{i}", "AI", both) for i in range(100)],
    )
    # Rooms are deliberately absent: with none at all every meeting is unroomed
    # wherever it goes, so the room term is a constant offset and the test is
    # about time and nothing else.
    return replace(snapshot, grid=DRIFT_GRID)


def _rank_drift(snapshot, board, section_id):
    starts = sorted(p.window.start for p in board.placements if p.section_id == section_id)
    legal = sorted({w.start for w in snapshot.grid.windows_for(75, DeliveryMode.IN_PERSON)})
    ranks = [legal.index(s) for s in starts]
    return max(ranks) - min(ranks)


def test_without_the_rule_a_section_is_taught_at_09_00_and_then_at_13_00():
    """The control. If this ever stops drifting the two tests below prove
    nothing, so it is asserted rather than assumed."""
    snapshot = _drift_snapshot()
    result = solve(snapshot, time_limit_seconds=20.0, max_time_of_day_slots=None)
    assert result.board.placements
    assert _rank_drift(snapshot, result.board, "lec#S1") == 2, (
        "the solver no longer prefers to scatter this section"
    )


def test_the_rule_keeps_a_section_within_one_slot_of_itself():
    snapshot = _drift_snapshot()
    result = solve(snapshot, time_limit_seconds=20.0, max_time_of_day_slots=1)
    assert result.board.placements, "a satisfiable ceiling must still yield a board"
    # {SUN,MON} and {MON,TUE} are both drift-1 and both cost the same clash, so
    # which one comes back is the solver's business. Asserting the drift rather
    # than the days keeps this insensitive to that tie -- do not "tighten" it to
    # name specific days, which would make it seed- and version-dependent.
    assert _rank_drift(snapshot, result.board, "lec#S1") <= 1


def test_a_rule_that_cannot_be_met_yields_no_board_rather_than_a_quiet_breach():
    """Every lecture time here exists on exactly one day, and a section may not
    meet twice in a day, so "the same slot every day" is impossible. The solver
    must say so rather than return a board that breaks the rule."""
    snapshot = _drift_snapshot()
    result = solve(snapshot, time_limit_seconds=20.0, max_time_of_day_slots=0)
    assert not result.board.placements


def test_the_ceiling_counts_slots_not_minutes():
    """The grid's lecture family runs 09:00, 10:30, 10:50, 13:00, 14:30, 14:45,
    16:00. Its smallest step is 15 minutes and its largest is 130, so a ceiling
    expressed in minutes cannot mean "one slot": set to the smallest it forbids
    09:00 -> 10:30, and set to the largest it permits 09:00 -> 13:00, which is
    the move being complained about."""
    grid = Grid.from_spec(
        lecture_starts={"09:00": 75, "10:30": 75, "10:50": 75, "13:00": 75},
        lab_starts={"09:00": 100},
    )
    snapshot = replace(_snapshot([_offering()], [Section("off1#S1", "off1", 1, 30)]), grid=grid)
    # 09:00 and 10:30 are neighbours in the declared list, 90 minutes apart.
    adjacent = Board(
        (
            _p(day=Day.SUN, window=TimeWindow(540, 615)),
            _p(idx=2, day=Day.MON, window=TimeWindow(630, 705)),
        )
    )
    assert time_of_day_drift(snapshot, adjacent)["within_one_slot"] == 1

    # 10:50 and 13:00 are also neighbours -- but 130 minutes apart, and across
    # lunch. Counted as within one slot, and the minute figures say why it is
    # still worth reporting separately.
    across_lunch = Board(
        (
            _p(day=Day.SUN, window=TimeWindow(650, 725)),
            _p(idx=2, day=Day.MON, window=TimeWindow(780, 855)),
        )
    )
    measured = time_of_day_drift(snapshot, across_lunch)
    assert measured["within_one_slot"] == 1
    assert measured["mean_drift_minutes"] == 130.0

    # 09:00 and 13:00 are three apart. No minute ceiling could separate this
    # from the 130-minute case above without also banning 09:00 -> 10:30.
    far = Board(
        (
            _p(day=Day.SUN, window=TimeWindow(540, 615)),
            _p(idx=2, day=Day.MON, window=TimeWindow(780, 855)),
        )
    )
    assert time_of_day_drift(snapshot, far)["within_one_slot"] == 0


def test_lectures_and_labs_are_not_required_to_match_each_other():
    """They are drawn from different declared families with different start
    times; demanding they line up would be a rule the grid cannot satisfy.

    This section is PERFECT on the rule as asked -- both its lectures are at
    13:00 -- and its lab is at 09:00 because 13:00 is not a lab time here.
    Comparing the two families would report it as wandering four hours.
    """
    offering = _offering(
        reqs=(
            MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 2),
            MeetingRequirement(MeetingKind.LAB, DeliveryMode.IN_PERSON, 100, 1),
        )
    )
    snapshot = _snapshot([offering], [Section("off1#S1", "off1", 1, 30)])
    board = Board(
        (
            _p(day=Day.SUN, window=W13),
            _p(idx=2, day=Day.MON, window=W13),
            _p(idx=3, day=Day.TUE, window=W100, kind=MeetingKind.LAB),
        )
    )
    measured = time_of_day_drift(snapshot, board)
    assert measured["sections_with_several_meetings"] == 1, "the lab was counted as a lecture"
    assert measured["same_slot"] == 1
    assert measured["mean_drift_minutes"] == 0.0


#: As DRIFT_GRID, plus a Wednesday lecture at 10:50 -- twenty minutes after the
#: Monday one. The lecture family's declared starts are then 09:00, 10:30,
#: 10:50, so its SMALLEST step is 20 minutes while "the next slot" from 09:00 is
#: 90 minutes away. The clashing lab moves to Wednesday, which makes Sunday +
#: Monday the right answer and Monday + Wednesday the wrong one.
_WED1050 = TimeWindow(650, 725)
_WED_LAB = TimeWindow(645, 745)

MINUTE_TRAP_GRID = Grid(
    slots=(
        Slot(Day.SUN, _SUN9, MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
        Slot(Day.MON, _MON1030, MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
        Slot(Day.WED, _WED1050, MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
        Slot(Day.WED, _WED_LAB, MeetingKind.LAB, DeliveryMode.IN_PERSON),
    )
)


def test_the_solver_reads_one_slot_as_the_next_slot_not_as_twenty_minutes():
    """A ceiling in minutes has to be set to the family's smallest step, and on
    an irregular grid that is not "one slot" -- it is whichever two starts
    happen to sit closest together.

    Here the only pair within twenty minutes is Monday + Wednesday, and
    Wednesday is where the hundred-student lab is. A minute-based ceiling would
    force the section into that clash while calling itself satisfied; counting
    slots leaves Sunday + Monday available, which is both one slot apart and
    clash-free.
    """
    snapshot = replace(_drift_snapshot(), grid=MINUTE_TRAP_GRID)
    result = solve(snapshot, time_limit_seconds=20.0, max_time_of_day_slots=1)
    assert result.board.placements
    starts = sorted(p.window.start for p in result.board.placements if p.section_id == "lec#S1")
    assert starts == [540, 630], "the section was pushed onto Wednesday, into the lab"


# ── choosing between boards ───────────────────────────────────────────────
#
# The planner runs several times from different seeds and keeps one board. That
# choice is a policy, not an optimisation, so it is stated as an order of
# preference and tested here directly rather than by producing real boards --
# which would cost minutes of solver time per comparison and still not let a
# test control what the two boards differ by.

from scheduler.solve import choose_run  # noqa: E402


def _run(name, *, days=(3, 3), unroomed=0, astray=0, idle=100, clashes=50.0):
    return {
        "result": name,
        "days": list(days),
        "unroomed": unroomed,
        "astray": astray,
        "metrics": {"idle_minutes": idle},
        "clashes": clashes,
    }


def test_nobody_works_a_longer_week_so_that_somebody_else_waits_less():
    chosen = choose_run(
        [
            _run("shorter weeks", days=(3, 3), idle=900),
            _run("one more day", days=(4, 3), idle=100),
        ]
    )
    assert chosen["result"] == "shorter weeks"


def test_a_day_moved_between_two_people_is_not_mistaken_for_neutral():
    """Both boards total six days. One of them makes somebody work five."""
    chosen = choose_run(
        [
            _run("even", days=(3, 3), idle=900),
            _run("lopsided", days=(5, 1), idle=100),
        ]
    )
    assert chosen["result"] == "even"


def test_a_class_with_nowhere_to_meet_outranks_everyone_s_waiting():
    chosen = choose_run(
        [
            _run("roomed", unroomed=0, idle=900, clashes=90.0),
            _run("five classes stranded", unroomed=5, idle=100, clashes=10.0),
        ]
    )
    assert chosen["result"] == "roomed"


def test_a_board_that_kept_the_same_hour_rule_beats_one_that_gave_it_up():
    """plan() drops the time-of-day ceiling when it cannot be met inside the time
    limit, so one seed can come back having kept the rule and another having
    abandoned it. Ranking those two on waiting alone would take the wrong one."""
    chosen = choose_run(
        [
            _run("rule kept", astray=0, idle=900),
            _run("rule dropped", astray=12, idle=100),
        ]
    )
    assert chosen["result"] == "rule kept"


def test_a_minute_of_waiting_does_not_outweigh_thirty_student_clashes():
    """Idle times within the band count as equal, and the tie goes to students."""
    chosen = choose_run(
        [
            _run("one minute better", idle=100, clashes=90.0),
            _run("thirty clashes better", idle=105, clashes=60.0),
        ]
    )
    assert chosen["result"] == "thirty clashes better"


def test_a_waiting_gap_wider_than_the_band_still_wins():
    """The band is a tolerance, not an excuse to stop caring about waiting."""
    chosen = choose_run(
        [
            _run("much less waiting", idle=100, clashes=90.0),
            _run("far more waiting", idle=400, clashes=60.0),
        ]
    )
    assert chosen["result"] == "much less waiting"


def test_the_earlier_preferences_are_not_overridden_by_the_later_ones():
    """Days first, then rooms, then the hour rule, then waiting -- each only
    breaks ties left by the one before."""
    chosen = choose_run(
        [
            _run("best on everything later", days=(4, 3), unroomed=0, astray=0, idle=10),
            _run("wins on days alone", days=(3, 3), unroomed=9, astray=9, idle=999),
        ]
    )
    assert chosen["result"] == "wins on days alone"


# ── the same-hour rule, the parts the first round of tests missed ─────────
#
# A mutation audit of the tests above found 17 of 18 mutations surviving. What
# follows kills the ones that got through. Each test names the mutation it
# exists for, because a test whose purpose is not written down gets weakened by
# the next person who finds it inconvenient.

from scheduler.solve import astray_count  # noqa: E402


def test_a_section_with_both_a_lecture_and_a_lab_is_planned_without_crashing():
    """MUTATION: group meetings by the offering's FIRST requirement rather than by
    each meeting's own. That crashes with `KeyError: 645` on any section holding
    two requirement families -- a 100-minute lab start looked up in the 75-minute
    lecture family's rank table -- and every test above stayed green, because not
    one of them gave a section more than one kind of meeting."""
    offering = _offering(
        reqs=(
            MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 2),
            MeetingRequirement(MeetingKind.LAB, DeliveryMode.IN_PERSON, 100, 2),
        )
    )
    snapshot = _snapshot([offering], [Section("off1#S1", "off1", 1, 30)])
    result = solve(snapshot, time_limit_seconds=20.0, max_time_of_day_slots=1)
    assert result.board.placements, "a lecture-plus-lab section produced no board"

    lectures = sorted(p.window.start for p in result.board.placements if p.window.duration == 75)
    labs = sorted(p.window.start for p in result.board.placements if p.window.duration == 100)
    assert len(lectures) == 2 and len(labs) == 2

    def spread(starts, duration):
        legal = sorted({w.start for w in GRID.windows_for(duration, DeliveryMode.IN_PERSON)})
        ranks = [legal.index(s) for s in starts]
        return max(ranks) - min(ranks)

    assert spread(lectures, 75) <= 1
    assert spread(labs, 100) <= 1


def test_two_starts_apart_is_outside_one_slot_however_few_minutes_it_is():
    """MUTATION: measure `within_one_slot` in minutes at any threshold from 130 to
    239. The boards the earlier test used sit 90, 130 and 240 minutes apart at
    ranks 1, 1 and 3, so every one of those thresholds reproduces all three of its
    assertions. The discriminating case is 09:00 and 10:50 -- only 110 minutes,
    but two declared starts apart -- and it was missing."""
    grid = Grid.from_spec(
        lecture_starts={"09:00": 75, "10:30": 75, "10:50": 75, "13:00": 75},
        lab_starts={"09:00": 100},
    )
    snapshot = replace(_snapshot([_offering()], [Section("off1#S1", "off1", 1, 30)]), grid=grid)
    board = Board(
        (
            _p(day=Day.SUN, window=TimeWindow(540, 615)),  # 09:00, rank 0
            _p(idx=2, day=Day.MON, window=TimeWindow(650, 725)),  # 10:50, rank 2
        )
    )
    measured = time_of_day_drift(snapshot, board)
    assert measured["mean_drift_minutes"] == 110.0, "these are 110 minutes apart"
    assert measured["within_one_slot"] == 0, "two declared starts apart is not one slot"


def test_the_reported_percentages_are_not_swapped_and_the_worst_is_the_worst():
    """MUTATION: swap `percent_same_slot` and `percent_within_one_slot` in the
    returned dict, or report the LAST section rather than the worst. Both survived
    everything above, and both are written to the saved plan and printed for an
    operator to act on."""
    reqs = _offering().requirements
    snapshot = _snapshot(
        [_offering(f"off{i}", reqs=reqs) for i in range(1, 5)],
        [Section(f"off{i}#S1", f"off{i}", 1, 30) for i in range(1, 5)],
    )
    # Deliberately four sections with THREE different outcomes, so the two
    # percentages come out different (equal ones cannot detect a swap) and the
    # worst wanderer is not also the last one seen (which cannot detect "report
    # whatever came last"). Sections are visited in id order.
    board = Board(
        (
            _p(day=Day.SUN, window=W9),  # off1: 09:00 / 09:00
            _p(idx=2, day=Day.MON, window=W9),  #   same hour, rank 0
            _p(section="off2#S1", offering="off2", day=Day.SUN, window=W9),
            _p(section="off2#S1", offering="off2", idx=2, day=Day.MON, window=W1030),
            _p(section="off3#S1", offering="off3", day=Day.SUN, window=W9),
            _p(section="off3#S1", offering="off3", idx=2, day=Day.MON, window=W13),
            _p(section="off4#S1", offering="off4", day=Day.SUN, window=W9),
            _p(section="off4#S1", offering="off4", idx=2, day=Day.MON, window=W1030),
        )
    )
    measured = time_of_day_drift(snapshot, board)
    assert measured["sections_with_several_meetings"] == 4
    assert measured["percent_same_slot"] == 25.0, "only off1 holds its exact hour"
    assert measured["percent_within_one_slot"] == 75.0, (
        "off3 spans ranks 0 and 2; the other three are within one slot"
    )
    assert measured["worst_minutes"] == 240
    assert measured["worst_section"] == "off3#S1", "off4 was seen last, but off3 wandered furthest"


# ── the ceiling as the planner actually ships it ──────────────────────────
#
# MUTATION: change `plan()`'s default from 1 to None. It survived the whole file,
# because every existing plan() test builds sections that meet ONCE a week -- so
# `if len(indexes) < 2: continue` skipped the block entirely, and the default that
# governs the bridge and the Generate button was never reached by any test.


def _twice_weekly_snapshot(grid=None):
    offerings = [_offering(f"off{i}") for i in range(1, 4)]  # each 2 x 75-min lectures
    sections = [Section(f"off{i}#S1", f"off{i}", 1, 30) for i in range(1, 4)]
    snapshot = _snapshot(offerings, sections)
    return replace(snapshot, grid=grid) if grid else snapshot


def test_the_planner_keeps_a_section_on_its_hour_without_being_asked():
    """The same fixture the solver-level test uses: left alone this section goes
    to 09:00 and 13:00 to dodge a hundred-student clash. A snapshot with no such
    pressure cannot detect the default being switched off, because the solver is
    then free to align the sections anyway and usually does."""
    snapshot = _drift_snapshot()
    result = plan(snapshot, time_limit_seconds=20.0)
    assert result.board.placements
    assert _rank_drift(snapshot, result.board, "lec#S1") <= 1, (
        "the default ceiling did not reach the planner"
    )


def test_the_planner_says_so_when_it_has_to_give_the_rule_up():
    """A grid where every lecture time exists on exactly one day, so "the same slot
    every day" is impossible. The planner must still return a timetable AND say
    what it abandoned -- as a warning, not buried among the routine notes that
    every successful run also writes."""
    snapshot = _twice_weekly_snapshot(DRIFT_GRID)
    result = plan(snapshot, time_limit_seconds=20.0, max_time_of_day_slots=0)
    assert result.board.placements, "an impossible preference must not cost the timetable"
    assert any("the rule was dropped" in warning for warning in result.warnings), (
        f"the compromise was not reported: warnings={result.warnings}"
    )
    assert not any("the rule was dropped" in note for note in result.notes), (
        "a compromise belongs in warnings, not competing with routine notes"
    )


def test_a_rule_that_can_be_met_produces_no_warning():
    """The other half: warnings have to stay rare enough to be worth reading."""
    result = plan(_twice_weekly_snapshot(), time_limit_seconds=20.0)
    assert result.board.placements
    assert result.warnings == []


# ── one slot apart can still be two hours apart ───────────────────────────


def test_the_minute_ceiling_forbids_the_one_slot_step_that_crosses_noon():
    """The rank ceiling alone leaves exactly one bad pair on the live grid:
    10:50 -> 13:00 is one declared slot, and 130 minutes across lunch. It was the
    worst case in all nine measured runs. `max_time_of_day_minutes` closes it, and
    no rank ceiling can."""
    # Each lecture time on ONE day only, so the days a section picks decide its
    # gap outright: Sunday+Monday is 10:30 and 10:50 (one slot, 20 minutes),
    # Monday+Tuesday is 10:50 and 13:00 (one slot, 130 minutes, across lunch).
    # The lab sits on Sunday and shares a hundred students, which is what makes
    # the solver prefer the noon-crossing pair when only rank is bounded.
    grid = Grid(
        slots=(
            Slot(Day.SUN, TimeWindow(630, 705), MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
            Slot(Day.MON, TimeWindow(650, 725), MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
            Slot(Day.TUE, TimeWindow(780, 855), MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
            Slot(Day.SUN, TimeWindow(645, 745), MeetingKind.LAB, DeliveryMode.IN_PERSON),
        )
    )
    snapshot = replace(_drift_snapshot(), grid=grid)

    loose = solve(snapshot, time_limit_seconds=20.0, max_time_of_day_slots=1)
    assert loose.board.placements
    assert time_of_day_drift(snapshot, loose.board)["worst_minutes"] == 130, (
        "this fixture exists to tempt the solver across noon; it did not, so the "
        "tightening below would prove nothing"
    )

    tight = solve(
        snapshot,
        time_limit_seconds=20.0,
        max_time_of_day_slots=1,
        max_time_of_day_minutes=100,
    )
    assert tight.board.placements, "100 minutes still leaves every ordinary step legal"
    assert time_of_day_drift(snapshot, tight.board)["worst_minutes"] <= 100


# ── ranking boards on the rule that was actually asked for ────────────────


def test_a_board_is_never_judged_against_a_ceiling_nobody_chose():
    """MUTATION: hardcode the comparison at one slot, as the first cut did. Under
    `--same-time-slots 2` a board that honoured the rule at spread 2 would then
    score WORSE than one that abandoned the rule and happened to land at spread 1;
    under `--same-time-slots 0` the two would be indistinguishable."""
    drift = {
        "sections_with_several_meetings": 10,
        "same_slot": 4,
        "within_one_slot": 7,
        "by_rank_spread": {0: 4, 1: 3, 2: 2, 4: 1},
    }
    assert astray_count(drift, None) == 0, "no rule was asked for, so none can be broken"
    assert astray_count(drift, 0) == 6, "at 0 the bar is the exact same hour"
    assert astray_count(drift, 1) == 3
    assert astray_count(drift, 2) == 1, "only the section four slots out breaks a ceiling of 2"
    assert astray_count(drift, 4) == 0

    # A plan stored before the histogram existed still ranks, at the two
    # ceilings the old fields can answer for.
    legacy = {"sections_with_several_meetings": 10, "same_slot": 4, "within_one_slot": 7}
    assert astray_count(legacy, 0) == 6
    assert astray_count(legacy, 1) == 3


def test_the_histogram_of_slot_spreads_adds_up_to_the_sections_measured():
    """It is what the portfolio now ranks on, so it has to agree with the
    headline figures rather than drift away from them."""
    reqs = _offering().requirements
    snapshot = _snapshot(
        [_offering(f"off{i}", reqs=reqs) for i in range(1, 4)],
        [Section(f"off{i}#S1", f"off{i}", 1, 30) for i in range(1, 4)],
    )
    board = Board(
        (
            _p(day=Day.SUN, window=W9),  # off1: spread 0 slots
            _p(idx=2, day=Day.MON, window=W9),
            _p(section="off2#S1", offering="off2", day=Day.SUN, window=W9),
            _p(section="off2#S1", offering="off2", idx=2, day=Day.MON, window=W1030),  # 1
            _p(section="off3#S1", offering="off3", day=Day.SUN, window=W9),
            _p(section="off3#S1", offering="off3", idx=2, day=Day.MON, window=W13),  # 2
        )
    )
    measured = time_of_day_drift(snapshot, board)
    assert measured["by_rank_spread"] == {0: 1, 1: 1, 2: 1}
    assert sum(measured["by_rank_spread"].values()) == measured["sections_with_several_meetings"]
    assert measured["by_rank_spread"][0] == measured["same_slot"]
    assert (
        measured["by_rank_spread"][0] + measured["by_rank_spread"][1] == measured["within_one_slot"]
    )


def test_rooms_still_outrank_the_same_hour_rule():
    """MUTATION: swap the rooms and same-hour criteria. It survived, because no
    test varied the two in one call -- a class with nowhere to meet cannot be
    taught at all, which outranks a section meeting at an awkward hour."""
    chosen = choose_run(
        [
            _run("roomed, rule broken", unroomed=0, astray=9),
            _run("rule kept, three stranded", unroomed=3, astray=0),
        ]
    )
    assert chosen["result"] == "roomed, rule broken"


def test_the_shortest_week_is_compared_person_by_person_all_the_way_down():
    """MUTATION: compare only the LONGEST week (`sorted(days)[:1]`). It survived,
    because no test had two boards whose longest weeks tie. (3,3) against (3,1) is
    that case, and the second is strictly kinder to the second instructor."""
    chosen = choose_run(
        [
            _run("three and one", days=(3, 1), idle=900),
            _run("three and three", days=(3, 3), idle=100),
        ]
    )
    assert chosen["result"] == "three and one"


def test_the_waiting_tolerance_is_narrow_enough_to_still_mean_something():
    """MUTATION: widen `idle_band` to 1.0. It survived, because the existing cases
    are 5% and 300% apart -- any band between those passes both. A board making
    instructors wait 40% longer is not "about the same"."""
    chosen = choose_run(
        [
            _run("forty percent more waiting", idle=140, clashes=10.0),
            _run("less waiting", idle=100, clashes=90.0),
        ]
    )
    assert chosen["result"] == "less waiting"


def test_waiting_breaks_a_tie_when_two_boards_cost_students_the_same():
    """MUTATION: drop `idle_minutes` from the final tie-break key. It survived,
    because no two runs had equal clashes."""
    chosen = choose_run(
        [
            _run("more waiting", idle=105, clashes=50.0),
            _run("less waiting", idle=100, clashes=50.0),
        ]
    )
    assert chosen["result"] == "less waiting"


# ── the confirmation metric must not flatter a board nobody can attend ────
#
# `clash_free_percent` is what the blueprint designates as the ground truth
# behind the clash PROXY, and it read 100% in two situations where it should
# have read nothing at all: a student with no seat has no clash and was counted
# in the numerator, and the failure path returns zeros, which is every student
# minus nobody. Review measured fifty students against a single seat reporting
# `clash_free_percent: 100.0` beside `unseated_demands: 49`.


def test_the_percentage_is_taken_over_the_students_who_actually_got_a_seat():
    """Not over everybody. The denominator is the whole claim."""
    snap = _two_course_snapshot(ROOMY, capacity=1)
    board = Board(
        (
            _p(section="offa#S1", offering="offa", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="offb#S1", offering="offb", day=Day.SUN, window=TimeWindow(630, 705)),
        )
    )
    result = seat_students(snap, board, time_limit_seconds=20)
    assert result.students == 10
    assert result.unseated_demands == 18, "nine of ten students are short both courses"
    assert result.students_fully_seated == 1, (
        f"one seat each means one complete week: got {result.students_fully_seated}"
    )
    # 100% is the honest answer HERE -- the one student who got a seat really
    # does have a clash-free week. What makes it readable is the denominator
    # beside it. Under the old formula this same board read "100% of 10", which
    # is a board nine students cannot attend described as a perfect week.
    assert result.clash_free_percent == 100.0
    summary = result.summary()
    assert summary["students_fully_seated"] == 1
    assert summary["students"] == 10, "both numbers must be published, or 100% lies"


def test_a_seating_solve_that_did_not_close_publishes_no_percentage_at_all():
    """`None` is loud; 100.0 is not.

    Deliberately a run that seated MOST students and still did not close --
    a fixture where nobody was seated cannot tell "we did not finish" apart from
    "there was nobody to count"."""
    result = SeatingResult(
        students=390,
        seated_demands=1800,
        unseated_demands=75,
        students_with_a_clash=0,
        total_clashes=0,
        idle_minutes=0,
        proven_optimal=False,
        students_fully_seated=350,
        notes=["no seating found within 1s"],
    )
    assert result.clash_free_percent is None, (
        "an unfinished solve must publish nothing, not the flattering number "
        "its partial assignment happens to imply"
    )
    assert result.summary()["clash_free_percent"] is None


def test_the_denominator_changes_the_answer_not_just_the_wording():
    """The two formulas have to be told apart by a case where they disagree.

    One seat per course and both courses at the SAME hour: exactly one student
    gets both classes, and that student clashes. Over the seated student that is
    0% clash-free, which is the truth. Over all ten students -- the old
    formula -- it reads 90%, because the nine with no seat at all have no clash
    to report."""
    # Two students, both needing both courses, both courses at the same hour.
    # offa holds two, offb holds one -- so three of the four demands can be met,
    # and whoever gets offb also has offa and therefore clashes. The other
    # student ends up with offa only, and is not fully seated.
    snap = _grid_snapshot(
        ROOMY,
        [_offering("offa", reqs=_lecture(1)), _offering("offb", reqs=_lecture(1))],
        [Section("offa#S1", "offa", 1, 2), Section("offb#S1", "offb", 1, 1)],
        demand=[
            StudentDemand(student_id=n, program="AI", offering_ids=frozenset({"offa", "offb"}))
            for n in (1, 2)
        ],
    )
    board = Board(
        (
            _p(section="offa#S1", offering="offa", day=Day.SUN, window=TimeWindow(540, 615)),
            _p(section="offb#S1", offering="offb", day=Day.SUN, window=TimeWindow(540, 615)),
        )
    )
    result = seat_students(snap, board, time_limit_seconds=20)
    assert result.students == 2
    assert result.unseated_demands == 1, "one of the four demands cannot be met"
    assert result.students_fully_seated == 1
    assert result.students_with_a_clash == 1
    assert result.clash_free_percent == 0.0, (
        "over everybody this reads 50% -- the student with no offb seat cannot clash"
    )


from scheduler.solve import expected_clashes  # noqa: E402

# ── the gap pass may not spend a clash-free board ─────────────────────────
#
# `plan()` bounds student harm by a ceiling derived from pass 1. It used to drop
# that ceiling whenever pass 1 scored zero -- justified for `alpha == 0`, where
# the clash booleans carry no objective pressure and their values are arbitrary,
# but zero also means pass 1 found a CLASH-FREE board, and dropping the ceiling
# then removed it exactly where it had the most to protect.


def _clash_free_but_gappy():
    """A day where keeping the instructor's classes together costs a student
    their clash-free week.

    Three 75-minute lecture times (09:00, 10:30, 16:00) and ONE 100-minute
    window (09:00-10:40) which straddles both morning lecture times. B is the
    100-minute course, so it has nowhere else to go. Twenty students need A and
    B, and the instructor teaches A and C.

    * clash-free demands A at 16:00, which leaves the instructor a hole between
      the morning and the late afternoon;
    * closing that hole means A at 09:00 or 10:30 -- both underneath B.

    So the gap pass can only shorten the instructor's day by clashing every one
    of those twenty students, and the ceiling is the only thing stopping it.
    """
    grid = Grid(
        slots=(
            Slot(Day.SUN, TimeWindow(540, 615), MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
            Slot(Day.SUN, TimeWindow(630, 705), MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
            Slot(Day.SUN, TimeWindow(960, 1035), MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
            Slot(Day.SUN, TimeWindow(540, 640), MeetingKind.LECTURE, DeliveryMode.IN_PERSON),
        )
    )
    long_lecture = (MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 100, 1),)
    offerings = [
        _offering("offa", reqs=_lecture(1)),
        _offering("offb", reqs=long_lecture),
        _offering("offc", reqs=_lecture(1)),
    ]
    sections = [
        Section("offa#S1", "offa", 1, 30, instructor_id=1),
        Section("offb#S1", "offb", 1, 30),
        Section("offc#S1", "offc", 1, 30, instructor_id=1),
    ]
    demand = [
        StudentDemand(student_id=n, program="AI", offering_ids=frozenset({"offa", "offb"}))
        for n in range(1, 21)
    ]
    snapshot = _snapshot_with(
        offerings, sections, [Instructor(id=1, name="Dr A", eligible_offerings=frozenset())]
    )
    return replace(snapshot, grid=grid, demand=tuple(demand))


def test_a_clash_free_board_is_not_spent_on_instructor_gaps():
    snapshot = _clash_free_but_gappy()
    result = plan(snapshot, time_limit_seconds=20.0)
    assert result.board.placements
    assert expected_clashes(snapshot, result.board) == 0.0, (
        "the gap pass turned a clash-free timetable into a clashing one, and "
        "reported it as a clean success"
    )
    # Note there is no "unbounded control" here, and there cannot be: when pass 1
    # scores zero, no `clash_tolerance` can loosen the ceiling, because any
    # multiple of zero is zero. The trade is real -- removing the ceiling lets
    # pass 2 take A down to 10:30 underneath B, buying 240 minutes of instructor
    # idle for twenty clashing students -- and the only way to observe that is to
    # remove the ceiling, which is exactly what the mutation test does.


def test_an_instructors_only_run_still_gets_its_second_pass():
    """The other half, and the reason the old condition existed. At alpha=0 the
    clash booleans carry no objective pressure, so their values are arbitrary and
    a ceiling built from them would be nonsense -- a ceiling of 0 would read as
    "no student may ever clash" and pass 2 would return nothing at all."""
    snapshot = _clash_free_but_gappy()
    result = plan(snapshot, time_limit_seconds=20.0, alpha=0.0)
    assert result.board.placements, "instructors-only lost its board to a nonsense ceiling"
    assert any("two-pass" in note for note in result.notes), f"pass 2 never ran: {result.notes}"
    # HONEST LIMIT: this does not detect "bound even at alpha=0". With students
    # out of the objective the clash booleans are only lower-bounded, so they
    # settle arbitrarily, and the ceiling derived from whatever they happened to
    # be is usually satisfiable -- pass 2 then runs and this test passes anyway.
    # Making it fire would need a fixture where those arbitrary values come out
    # exactly zero, which is a property of the solver's search rather than of
    # the data, and a test that depends on that is a test that will flake.
