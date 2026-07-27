"""Tests for the scheduler rulebook, independent checker and reference placer (S2).

The rules are pure functions over a snapshot and a board, so almost everything
here runs without a database. Each hard rule gets two tests: it must pass a clean
board *and* catch a deliberately broken one. A rule that only ever sees valid
input is indistinguishable from a rule that does nothing.
"""

from __future__ import annotations

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

from scheduler.solve import _SCALE, _days_by_instructor, plan, solve  # noqa: E402

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
