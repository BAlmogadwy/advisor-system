"""``TIMETABLE_INSTRUCTOR_AVAILABILITY``: per-instructor teaching-time rules in the
classic engine (slice B1: no automatic stage ever CREATES a violation).

The rules (``InstructorTimeRule``, semantics in ``core.services.instructor_availability``)
bind the teacher one-teacher-per-section charges to a section. This module pins:

- the flag (default OFF) and the one helper every stage asks
  (``core.services.timetable_instructor_availability``): who may read the flag, one
  rule query per scope, the teacher universe the rules are loaded for;
- the pure predicates (``timetable_constraints``): whole-board count and list, and
  the delta form a mover uses (a pre-existing violation may stay);
- byte-identity: flag ON with no rule, or with rules only for instructors the
  scenario cannot charge, reproduces the recorded goldens;
- the teacher choice (``section_teacher_assignment``): a teacher is never newly
  picked for a section at a time his rules forbid; a charged violator is kept;
- the greedy placer (``timetable_autoplace``): per-teacher options feed it, an
  engine teacher's forbidden option is filtered (``INSTRUCTOR_UNAVAILABLE``), a
  fixed teacher's ranks last (placed, reported), online sections are exempt.

Every behavioural test has a control (flag off, an empty availability, or the
guard taken out) proving the guard is what makes it pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from core.models import DeliveryBoard, Instructor, ProgrammeRequirement
from core.services import timetable_constraints as constraints
from core.services import timetable_instructor_availability as helper
from core.services.instructor_availability import (
    EMPTY_AVAILABILITY,
    KIND_AVAILABLE_ONLY,
    KIND_UNAVAILABLE,
    TimeRule,
    compile_rules,
    mask_from_days,
)
from core.services.timetable_assignment_models import SectionMeeting, SectionState
from tests import one_teacher_support as ots
from tests._instructor_availability_fixture import (
    AVAILABILITY_FLAGS,
    rule,
    rule_queries,
)
from tests.test_section_day_spread import GREEDY_CASES as DAY_SPREAD_GREEDY_CASES

REPO = Path(__file__).resolve().parent.parent
SUN, MON, TUE, WED, THU = range(5)


def _av(*rules: tuple[int, str, tuple[str, ...], int, int]):
    """A compiled availability from ``(instructor, kind, days, start, end)`` rows."""
    return compile_rules(
        TimeRule(iid, kind, mask_from_days(days), start, end)
        for iid, kind, days, start, end in rules
    )


#: Teacher 7 "no mornings" (cannot teach before 13:00 on any day).
NO_MORNINGS = _av((7, KIND_UNAVAILABLE, ("SUN", "MON", "TUE", "WED", "THU"), 0, 13 * 60))


@pytest.fixture
def av_on(settings):
    for name, value in AVAILABILITY_FLAGS.items():
        setattr(settings, name, value)
    return settings


@pytest.fixture
def av_off(av_on):
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    return av_on


# ── T-D1: the flag, default OFF ───────────────────────────────────────


def test_the_flag_is_off_by_default(settings) -> None:
    from core.services import timetable_flags
    from core.services.timetable_flags import is_instructor_availability_enabled

    source = (REPO / "config" / "settings.py").read_text(encoding="utf-8")
    assert re.search(
        r"TIMETABLE_INSTRUCTOR_AVAILABILITY = os\.getenv\(\s*"
        r'"TIMETABLE_INSTRUCTOR_AVAILABILITY",\s*"false"',
        source,
    )
    del settings.TIMETABLE_INSTRUCTOR_AVAILABILITY
    assert is_instructor_availability_enabled() is False
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = True
    assert is_instructor_availability_enabled() is True
    assert {"INSTRUCTOR_AVAILABILITY_SETTING", "is_instructor_availability_enabled"} <= set(
        timetable_flags.__all__
    )


def test_the_rejection_code_is_a_stable_sentinel() -> None:
    from core.services.timetable_decision_trace import INSTRUCTOR_UNAVAILABLE

    assert INSTRUCTOR_UNAVAILABLE == "INSTRUCTOR_UNAVAILABLE"


# ── T-H1..H5: the helper ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("flag", "one_teacher", "links", "expected"),
    [
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),  # one teacher per section needs the links flag
        (True, True, True, True),
    ],
)
def test_rules_need_the_flag_and_one_teacher_per_section(
    settings, flag, one_teacher, links, expected
) -> None:
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = one_teacher
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = links

    assert helper.rules_enabled() is expected


def _course_scenario(name: str, teachers: dict[str, str], *, code: str = "CS101"):
    """A one-board scenario with one budgeted course, linked to ``teachers``
    (``{full name: programme}``); returns ``(scenario, {name: Instructor})``."""
    sc, _board = ots.scenario(name, programs=("AI",))
    key = ots.requirement("AI", code, f"COURSE {code}")
    ots.budget(sc, key, 2)
    people = {}
    for full_name in teachers:
        people[full_name] = Instructor.objects.filter(
            full_name=full_name
        ).first() or ots.instructor(full_name)
        ots.link("AI", code, people[full_name])
    return sc, people


@pytest.mark.django_db
def test_rules_off_mean_an_empty_availability_and_no_query(av_off) -> None:
    sc, people = _course_scenario("off", {"Dr A": "AI"})
    rule(people["Dr A"], KIND_UNAVAILABLE)

    with CaptureQueriesContext(connection) as ctx:
        av = helper.availability_for(sc.id)

    assert av is EMPTY_AVAILABILITY
    assert ctx.captured_queries == []


@pytest.mark.django_db
def test_one_rule_query_per_scope_and_the_same_object(av_on) -> None:
    sc, people = _course_scenario("scope", {"Dr A": "AI"})
    rule(people["Dr A"], KIND_UNAVAILABLE)
    universe = {people["Dr A"].pk}

    with CaptureQueriesContext(connection) as ctx:
        with helper.availability_scope(sc.id):
            first = helper.availability_for(sc.id, universe=universe)
            second = helper.availability_for(sc.id, universe=universe)
    assert first is second
    assert first.instructor_ids == {people["Dr A"].pk}
    assert len(rule_queries(ctx.captured_queries)) == 1

    # Control: unbound, each call loads.
    with CaptureQueriesContext(connection) as ctx:
        helper.availability_for(sc.id, universe=universe)
        helper.availability_for(sc.id, universe=universe)
    assert len(rule_queries(ctx.captured_queries)) == 2


@pytest.mark.django_db
def test_scopes_are_re_entrant(av_on) -> None:
    sc, people = _course_scenario("nested", {"Dr A": "AI"})
    rule(people["Dr A"], KIND_UNAVAILABLE)
    universe = {people["Dr A"].pk}

    with CaptureQueriesContext(connection) as ctx:
        with helper.availability_scope(sc.id):
            outer = helper.availability_for(sc.id, universe=universe)
            with helper.availability_scope(sc.id), helper.availability_scope(None):
                inner = helper.availability_for(sc.id, universe=universe)
        with helper.availability_scope(None):
            first = helper.availability_for(sc.id, universe=universe)
            with helper.availability_scope(sc.id):
                again = helper.availability_for(sc.id, universe=universe)
    assert outer is inner and first is again
    assert len(rule_queries(ctx.captured_queries)) == 2


@pytest.mark.django_db
def test_a_scenario_scope_opened_inside_a_request_scope_shares_its_holder(av_on) -> None:
    """foundation:F6 (M37): a run's ``availability_scope(sid)`` opened inside a
    request's ``availability_scope(None)`` BEFORE anything is loaded reuses the
    request's holder, so a read after the run, in the same request, is not a
    second rule query."""
    sc, people = _course_scenario("nested first", {"Dr A": "AI"})
    rule(people["Dr A"], KIND_UNAVAILABLE)
    universe = {people["Dr A"].pk}

    with CaptureQueriesContext(connection) as ctx:
        with helper.availability_scope(None):
            with helper.availability_scope(sc.id):
                during = helper.availability_for(sc.id, universe=universe)
            after = helper.availability_for(sc.id, universe=universe)
    assert during is after
    assert len(rule_queries(ctx.captured_queries)) == 1


@pytest.mark.django_db
def test_rules_are_loaded_only_for_the_teacher_universe(av_on) -> None:
    sc, people = _course_scenario("universe", {"Dr A": "AI"})
    outsider = ots.instructor("Dr Zed")
    rule(people["Dr A"], KIND_UNAVAILABLE, start="09:00", end="10:00")
    rule(outsider, KIND_UNAVAILABLE)

    av = helper.availability_for(sc.id, universe={people["Dr A"].pk})

    assert av.instructor_ids == {people["Dr A"].pk}
    # Control: the same rows, loaded for both, do hold the outsider.
    assert helper.availability_for(
        sc.id, universe={people["Dr A"].pk, outsider.pk}
    ).instructor_ids == {people["Dr A"].pk, outsider.pk}


@pytest.mark.django_db
def test_without_a_universe_the_scenario_map_gives_it_in_one_rule_query(av_on) -> None:
    sc, people = _course_scenario("own universe", {"Dr A": "AI"})
    outsider = ots.instructor("Dr Zed")
    rule(people["Dr A"], KIND_UNAVAILABLE, start="09:00", end="10:00")
    rule(outsider, KIND_UNAVAILABLE)

    for scoped in (False, True):
        with CaptureQueriesContext(connection) as ctx:
            if scoped:
                with helper.availability_scope(sc.id):
                    av = helper.availability_for(sc.id)
            else:
                av = helper.availability_for(sc.id)
        assert av.instructor_ids == {people["Dr A"].pk}
        assert len(rule_queries(ctx.captured_queries)) == 1, scoped


@pytest.mark.django_db
def test_a_universe_that_grows_inside_a_scope_loads_the_new_teachers_rules(av_on) -> None:
    """D-G (foundation:F2): the scope remembers the universe it loaded. A later
    read with ids outside it loads the rules of the union once more (one query);
    a read inside what is loaded costs nothing. Unscoped there is nothing to
    grow: each read loads its own universe."""
    sc, people = _course_scenario("grows", {"Dr A": "AI"})
    late = ots.instructor("Dr Late")
    rule(people["Dr A"], KIND_UNAVAILABLE, ("SUN",))
    rule(late, KIND_UNAVAILABLE, ("MON",))
    a, t = people["Dr A"].pk, late.pk

    with CaptureQueriesContext(connection) as ctx:
        with helper.availability_scope(sc.id):
            first = helper.availability_for(sc.id, universe={a})
            checker = helper.section_rule_checker(sc.id)
            grown = helper.availability_for(sc.id, universe={t})
            again = helper.availability_for(sc.id, universe={a, t})
            plain = helper.availability_for(sc.id)
            rebuilt = helper.section_rule_checker(sc.id)

    assert first.instructor_ids == {a}
    assert grown.instructor_ids == {a, t}  # the union, not the new ids alone
    assert again is grown and plain is grown
    # The writers' checker was built from the smaller availability: rebuilt.
    assert checker.availability is first and rebuilt.availability is grown
    assert len(rule_queries(ctx.captured_queries)) == 2


@pytest.mark.django_db
@pytest.mark.parametrize("read_first", [True, False], ids=["read-before-create", "control"])
def test_a_manual_create_checks_a_teacher_who_enters_the_universe_mid_request(
    av_on, read_first
) -> None:
    """D-G (foundation:F2): an own CS300 section (no budget) is not in the teacher
    universe until it is placed. A read earlier in the same request loads the
    rules for the universe WITHOUT its only teacher Dr T; the manual create's
    closure must still see his "no Sundays" and leave the section TBD instead of
    storing him at a forbidden SUN 09:00. Control: no earlier read."""
    from core.models import SectionInstructor
    from core.services.course_identity import planner_course_key
    from core.services.timetable_workspace import validate_placement

    sc, board, _key, _ts, (_a,) = _ruled_board(eligible=("Dr A",))
    ProgrammeRequirement.objects.create(
        program="AI", course_code="CS300", course_name="OTHER", programme_term=2, credit_hours=3
    )
    teacher = ots.instructor("Dr T")
    ots.link("AI", "CS300", teacher)
    other = planner_course_key("CS300", "OTHER")
    ts = ots.section(sc, other, "S1")
    rule(teacher, KIND_UNAVAILABLE, ("SUN",))

    with CaptureQueriesContext(connection) as ctx, sta.request_scope():
        if read_first:
            validate_placement(board.id, "SUN", "09:00", "10:15", "", ts.pk)
            assert len(rule_queries(ctx.captured_queries)) == 1  # the universe without Dr T
        ots.place(board, ts, [("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15")])
        sta.name_new_section(sc.id, ts, board)

    stored = SectionInstructor.objects.filter(scenario=sc, course_key=other, section="S1")
    assert list(stored.values_list("instructor_id", flat=True)) == []
    assert len(rule_queries(ctx.captured_queries)) == (2 if read_first else 1)


@pytest.mark.django_db
def test_one_request_scope_keeps_one_availability_per_scenario(av_on) -> None:
    one, people_one = _course_scenario("first", {"Dr A": "AI"}, code="CS101")
    two, people_two = _course_scenario("second", {"Dr B": "AI"}, code="CS102")
    rule(people_one["Dr A"], KIND_UNAVAILABLE)
    rule(people_two["Dr B"], KIND_UNAVAILABLE)

    with helper.availability_scope(None):
        first = helper.availability_for(one.id, universe={people_one["Dr A"].pk})
        second = helper.availability_for(two.id, universe={people_two["Dr B"].pk})
        assert helper.availability_for(one.id) is first
        assert helper.bound_availability(two.id) is second
        assert helper.bound_availability() is EMPTY_AVAILABILITY  # two loaded: ambiguous

    assert first.instructor_ids == {people_one["Dr A"].pk}
    assert second.instructor_ids == {people_two["Dr B"].pk}


@pytest.mark.django_db
def test_bound_availability_never_loads(av_on) -> None:
    sc, people = _course_scenario("bound", {"Dr A": "AI"})
    rule(people["Dr A"], KIND_UNAVAILABLE)

    with CaptureQueriesContext(connection) as ctx:
        with helper.availability_scope(sc.id):
            before = helper.bound_availability()
            loaded = helper.availability_for(sc.id, universe={people["Dr A"].pk})
            after = helper.bound_availability()
    assert before is EMPTY_AVAILABILITY
    assert after is loaded and after
    assert helper.bound_availability() is EMPTY_AVAILABILITY  # the scope is closed
    assert len(rule_queries(ctx.captured_queries)) == 1


# ── the per-section predicates ────────────────────────────────────────


def test_rule_ids_keeps_only_ruled_integer_ids() -> None:
    assert helper.rule_ids(NO_MORNINGS, [9, 7, "7", True, None]) == (7,)
    assert helper.rule_ids(NO_MORNINGS, [9]) == ()
    assert helper.rule_ids(EMPTY_AVAILABILITY, [7]) == ()
    # True == 1: a bool must never pass for instructor 1.
    one = _av((1, KIND_UNAVAILABLE, ("SUN",), 0, 60))
    assert helper.rule_ids(one, [True]) == ()
    assert helper.rule_ids(one, [1]) == (1,)


def test_option_and_cell_predicates_read_the_placer_shapes() -> None:
    morning = [{"day": "SUN", "start": "09:00", "end": "10:15"}]
    afternoon = [{"day": "sun", "start": "13:00", "end": "14:15"}]

    assert helper.option_blocked(NO_MORNINGS, (7,), morning) is True
    assert helper.option_blocked(NO_MORNINGS, (7,), afternoon) is False
    assert helper.option_blocked(NO_MORNINGS, (9,), morning) is False
    assert helper.option_blocked(EMPTY_AVAILABILITY, (7,), morning) is False
    # Touching 13:00 is not overlapping.
    assert helper.meetings_blocked(NO_MORNINGS, (7,), [(MON, 11 * 60 + 45, 13 * 60)]) is True
    assert helper.meetings_blocked(NO_MORNINGS, (7,), [(MON, 13 * 60, 14 * 60)]) is False

    blocked = helper.cell_predicate(NO_MORNINGS, (9, 7))
    assert blocked("TUE", "10:30", "11:45") is True
    assert blocked("TUE", "13:00", "14:15") is False
    assert blocked("FRI", "10:30", "11:45") is False
    assert helper.cell_predicate(NO_MORNINGS, (9,)) is None
    assert helper.cell_predicate(EMPTY_AVAILABILITY, (7,)) is None


def test_prune_relocate_domains_drops_only_a_ruled_teachers_blocked_cells() -> None:
    def cell(day: str, start: str, end: str) -> dict:
        return {"day": day, "start": start, "end": end}

    am, pm = cell("SUN", "09:00", "10:15"), cell("SUN", "13:00", "14:15")
    am_bundle = [cell("SUN", "09:00", "12:00"), cell("MON", "09:00", "12:00")]
    pm_bundle = [cell("SUN", "13:00", "16:00"), cell("MON", "13:00", "16:00")]
    sections = [
        {"course_key": "A", "label": "S1"},  # ruled teacher 7
        {"course_key": "B", "label": "S1"},  # teacher 9, no rule
        {"course_key": "C", "label": "S1"},  # ruled, online everywhere
        {
            "course_key": "E",
            "label": "S1",
            "meeting_policy": object(),
            "bundles": [am_bundle, pm_bundle],
        },
    ]
    keep_b = [am, pm]
    valid = {0: [[am, pm]], 1: [keep_b], 2: [[am, pm]]}
    charged = {"A|S1": {7}, "B|S1": {9}, "C|S1": {7}, "E|S1": {7}}

    helper.prune_relocate_domains(NO_MORNINGS, sections, valid, charged, exempt={("C", "S1")})

    assert valid[0] == [[pm]]
    assert valid[1][0] is keep_b  # the very same list: an unruled section's draws are unchanged
    assert valid[2] == [[am, pm]]
    assert sections[3]["bundles"] == [pm_bundle]

    # Control: an empty availability touches nothing.
    valid = {0: [[am, pm]]}
    helper.prune_relocate_domains(EMPTY_AVAILABILITY, sections[:1], valid, charged)
    assert valid[0] == [[am, pm]]


# ── T-K1..K5: the pure board predicates ───────────────────────────────


def _state(section_id: str, meetings) -> SectionState:
    return SectionState(
        section_id=section_id,
        course_code=section_id.split("_", 1)[0],
        meetings=[SectionMeeting(day, start, end) for day, start, end in meetings],
        max_capacity=30,
        reserve_capacity=0,
        room_type_required="lecture",
        demand_capacity=30,
    )


AM = (9 * 60, 10 * 60 + 15)
PM = (13 * 60, 14 * 60 + 15)


def _board():
    return {
        # two distinct morning meetings, plus a board copy of one of them
        "X_S1": _state("X_S1", [(SUN, *AM), (TUE, *AM), (TUE, *AM)]),
        "Y_S1": _state("Y_S1", [(SUN, *PM), (TUE, *PM)]),
        "Z_S1": _state("Z_S1", [(MON, *AM), (WED, *PM)]),
    }


def test_count_and_list_blocked_distinct_meetings_of_charged_sections() -> None:
    board = _board()
    charged = {"X_S1": frozenset({7}), "Y_S1": frozenset({7}), "Z_S1": frozenset({9})}

    assert constraints.count_instructor_availability_violations(board, charged, NO_MORNINGS) == 2
    assert constraints.list_instructor_availability_violations(board, charged, NO_MORNINGS) == [
        {
            "section_id": "X_S1",
            "instructor_id": 7,
            "day": SUN,
            "start_min": AM[0],
            "end_min": AM[1],
        },
        {
            "section_id": "X_S1",
            "instructor_id": 7,
            "day": TUE,
            "start_min": AM[0],
            "end_min": AM[1],
        },
    ]
    assert constraints.section_availability_violations(board["X_S1"], {7}, NO_MORNINGS) == 2
    # Control: Z's morning meeting is a rule break once teacher 7 is charged with it.
    assert (
        constraints.count_instructor_availability_violations(
            board, {**charged, "Z_S1": frozenset({7})}, NO_MORNINGS
        )
        == 3
    )
    assert (
        constraints.count_instructor_availability_violations(board, charged, EMPTY_AVAILABILITY)
        == 0
    )


def test_exempt_sections_are_never_counted() -> None:
    board = _board()
    charged = {"X_S1": frozenset({7})}

    assert (
        constraints.count_instructor_availability_violations(
            board, charged, NO_MORNINGS, exempt={"X_S1"}
        )
        == 0
    )
    assert (
        constraints.list_instructor_availability_violations(
            board, charged, NO_MORNINGS, exempt={"X_S1"}
        )
        == []
    )
    assert (
        constraints.move_adds_availability_violation(
            board, charged, NO_MORNINGS, {"X_S1": [SectionMeeting(SUN, *PM)]}, exempt={"X_S1"}
        )
        is False
    )


def test_a_move_into_a_blocked_time_adds_a_violation() -> None:
    board = _board()
    charged = {"Y_S1": frozenset({7})}
    before = {"Y_S1": list(board["Y_S1"].meetings)}
    board["Y_S1"].meetings[0] = SectionMeeting(SUN, *AM)

    assert constraints.move_adds_availability_violation(board, charged, NO_MORNINGS, before)
    # Controls: an empty availability, or a teacher without a rule, never objects.
    assert not constraints.move_adds_availability_violation(
        board, charged, EMPTY_AVAILABILITY, before
    )
    assert not constraints.move_adds_availability_violation(
        board, {"Y_S1": frozenset({9})}, NO_MORNINGS, before
    )


def test_a_section_already_breaking_a_rule_may_move_without_breaking_it_more() -> None:
    board = _board()
    charged = {"X_S1": frozenset({7})}
    before = {"X_S1": list(board["X_S1"].meetings)}
    # SUN+TUE mornings (2 blocked) -> MON morning + WED afternoon (1 blocked): allowed.
    board["X_S1"].meetings[:] = [SectionMeeting(MON, *AM), SectionMeeting(WED, *PM)]
    assert not constraints.move_adds_availability_violation(board, charged, NO_MORNINGS, before)

    # From one blocked meeting to another single blocked meeting: still allowed (delta).
    before = {"X_S1": [SectionMeeting(MON, *AM), SectionMeeting(WED, *PM)]}
    board["X_S1"].meetings[:] = [SectionMeeting(THU, *AM), SectionMeeting(WED, *PM)]
    assert not constraints.move_adds_availability_violation(board, charged, NO_MORNINGS, before)

    # Control: one more blocked meeting than before is refused.
    board["X_S1"].meetings[:] = [SectionMeeting(THU, *AM), SectionMeeting(WED, *AM)]
    assert constraints.move_adds_availability_violation(board, charged, NO_MORNINGS, before)


def test_online_exemption_needs_online_on_every_board() -> None:
    assert helper.online_everywhere([True, True]) is True
    assert helper.online_everywhere([True, False]) is False
    assert helper.online_everywhere([]) is False


@pytest.mark.django_db
def test_a_section_online_on_one_of_its_two_boards_is_rule_checked(av_on) -> None:
    sc, ai_board = ots.scenario("online", programs=("AI", "DS"), board_program="AI")
    ds_board = DeliveryBoard.objects.create(
        scenario=sc, label="Term 2 DS", nominal_term=2, program="DS"
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="GS101",
        course_name="CULTURE",
        programme_term=2,
        credit_hours=2,
        is_online=True,
    )
    ProgrammeRequirement.objects.create(
        program="DS", course_code="GS101", course_name="CULTURE", programme_term=2, credit_hours=2
    )
    from core.services.course_identity import planner_course_key

    key = planner_course_key("GS101", "CULTURE")
    shared = ots.section(sc, key, "S1")
    online_only = ots.section(sc, key, "S2")
    ots.place(ai_board, shared, [("SUN", "09:00", "10:15")])
    ots.place(ds_board, shared, [("SUN", "09:00", "10:15")])
    ots.place(ai_board, online_only, [("MON", "09:00", "10:15")])

    exempt = helper.online_exempt_sections(sc.id)
    flags = helper.placed_online_flags(sc.id)

    assert (key, "S1") not in exempt
    assert flags[shared.pk] == (False, True)
    # Control: online on its only board, it is exempt.
    assert (key, "S2") in exempt
    assert flags[online_only.pk] == (True,)


# ── T-P1a/b: flag ON without a rule the scenario can charge = the goldens ──


def _no_rules() -> None:
    return None


def _rules_outside_the_universe() -> None:
    rule(ots.instructor("Dr Zed"), KIND_UNAVAILABLE)  # unlinked: never in any universe
    rule(ots.instructor("Dr Yara"), KIND_AVAILABLE_ONLY, ("THU",), "13:00", "14:00")


RULE_SETS = {
    "flag_on_without_rules": _no_rules,
    "flag_on_rules_outside_the_universe": _rules_outside_the_universe,
}


@pytest.fixture
def day_spread_golden_flags(settings):
    """The day-spread golden's flags (recorded before this flag existed), plus this flag ON."""
    from tests.test_section_day_spread import PRODUCTION_FLAGS

    for name, value in PRODUCTION_FLAGS.items():
        setattr(settings, name, value)
    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = True
    return settings


@pytest.fixture
def online_golden_flags(settings):
    """The online-windows golden's flags (windows OFF), plus this flag ON."""
    from tests._online_windows_fixture import ONLINE_FLAGS

    for name, value in ONLINE_FLAGS.items():
        setattr(settings, name, value)
    settings.TIMETABLE_ONLINE_EVENING_WINDOWS = False
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = True
    return settings


@pytest.mark.django_db
@pytest.mark.parametrize("rules", sorted(RULE_SETS))
@pytest.mark.parametrize(("strategy", "seed"), DAY_SPREAD_GREEDY_CASES)
def test_flag_on_greedy_build_matches_the_day_spread_golden(
    day_spread_golden_flags, rules, strategy, seed
) -> None:
    from tests.test_section_day_spread import _case_key, run_greedy_case
    from tests.test_section_day_spread import _golden as day_spread_golden

    RULE_SETS[rules]()

    assert (
        run_greedy_case(strategy, seed)
        == day_spread_golden()[f"greedy:{_case_key(strategy, seed)}"]
    )


@pytest.mark.django_db
@pytest.mark.parametrize("rules", sorted(RULE_SETS))
def test_flag_on_cpsat_models_match_the_day_spread_golden(
    day_spread_golden_flags, monkeypatch, rules
) -> None:
    from tests.test_one_teacher_flag_parity import build_corpus
    from tests.test_section_day_spread import _golden as day_spread_golden
    from tests.test_section_day_spread import _model_fingerprint, make_capture_models

    RULE_SETS[rules]()
    produced = {
        f"cpsat:{name}": _model_fingerprint(model)
        for name, model in make_capture_models(monkeypatch)(build_corpus(11)).items()
    }

    golden = day_spread_golden()
    assert produced == {key: golden[key] for key in produced}


@pytest.mark.django_db
@pytest.mark.parametrize("rules", sorted(RULE_SETS))
@pytest.mark.parametrize("corpus", ["plain", "online"])
def test_flag_on_relocate_repair_and_guard_domains_match_the_online_golden(
    online_golden_flags, monkeypatch, rules, corpus
) -> None:
    """Annealing / load-balancer relocate domains, cap-repair slot domains and the
    one-teacher guard's options."""
    from tests._online_windows_fixture import build_online_corpus, capture_domains
    from tests.test_one_teacher_flag_parity import build_corpus

    golden = json.loads(
        (REPO / "tests" / "fixtures" / "online_evening_windows_flag_off_golden.json").read_text(
            encoding="utf-8"
        )
    )
    RULE_SETS[rules]()
    builder = build_corpus if corpus == "plain" else build_online_corpus

    assert capture_domains(monkeypatch, builder(11)) == golden[f"{corpus}:domains"]


# ── T-P1c: a whole Optimise Current run ───────────────────────────────


class _Rollback(Exception):
    pass


def _key_paths(value, prefix: str = "") -> set[str]:
    """Every key path of a JSON-like result; id-like keys (digits) collapse to ``#``."""
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            name = "#" if str(key).lstrip("-").isdigit() else str(key)
            path = f"{prefix}.{name}"
            paths.add(path)
            paths |= _key_paths(inner, path)
    elif isinstance(value, list | tuple):
        for inner in value:
            paths |= _key_paths(inner, f"{prefix}[]")
    return paths


def _current_run(settings, flag: bool, rules, *, after_corpus=None) -> tuple[dict, set[str]]:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from tests.test_one_teacher_flag_parity import build_corpus, capture_board

    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    out: dict = {}
    try:
        with transaction.atomic():
            rules()
            scenario = build_corpus(11)
            if after_corpus is not None:
                after_corpus()
            auto_place_scenario(scenario.id, strategy="compact")
            result = run_v2_optimisation_guarded(
                scenario.id, mode="current", max_iterations=2, run_chain=False, run_cpsat=False
            )
            out["board"] = json.loads(json.dumps(capture_board(scenario), default=str))
            out["keys"] = _key_paths(json.loads(json.dumps(result, default=str)))
            out["keys"].add(f"safety_blocked={result.get('safety_blocked')}")
            raise _Rollback
    except _Rollback:
        pass
    return out["board"], out["keys"]


@pytest.mark.django_db
@pytest.mark.parametrize("rules", sorted(RULE_SETS))
def test_flag_on_optimise_current_run_matches_flag_off(day_spread_golden_flags, rules) -> None:
    off_board, off_keys = _current_run(day_spread_golden_flags, False, RULE_SETS[rules])
    on_board, on_keys = _current_run(day_spread_golden_flags, True, RULE_SETS[rules])

    assert on_board == off_board
    assert on_keys == off_keys


# ── C.1: the generator with a teacher's rules (the per-teacher option source) ──

#: Teacher 7 may teach only SUN 13:00-17:15 and TUE 10:30-12:05: no single
#: period is open on two days, so only a mixed-period week fits a 2 x 75 course.
SUN_PM_AND_TUE_LATE_MORNING = _av(
    (7, KIND_AVAILABLE_ONLY, ("SUN",), 13 * 60, 17 * 60 + 15),
    (7, KIND_AVAILABLE_ONLY, ("TUE",), 10 * 60 + 30, 12 * 60 + 5),
)
MIXED_WEEK = (("SUN", "13:00", "14:15"), ("TUE", "10:30", "11:45"))


def _weeks(options) -> set[tuple]:
    return {tuple((m["day"], m["start"], m["end"]) for m in option) for option in options}


def test_the_generator_builds_mixed_period_weeks_inside_a_teachers_rules() -> None:
    from core.services.timetable_autoplace import (
        DEFAULT_LAB_SLOTS,
        DEFAULT_SLOTS,
        _generate_meeting_options,
    )

    blocked = helper.cell_predicate(SUN_PM_AND_TUE_LATE_MORNING, (7,))
    options = _generate_meeting_options(
        [75, 75], DEFAULT_SLOTS, DEFAULT_LAB_SLOTS, [], cell_blocked=blocked
    )

    assert MIXED_WEEK in _weeks(options)
    assert all(not helper.option_blocked(SUN_PM_AND_TUE_LATE_MORNING, (7,), o) for o in options)
    # Control: without the rules every week keeps one period, so no mixed week.
    plain = _generate_meeting_options([75, 75], DEFAULT_SLOTS, DEFAULT_LAB_SLOTS, [])
    assert MIXED_WEEK not in _weeks(plain)
    assert any(helper.option_blocked(SUN_PM_AND_TUE_LATE_MORNING, (7,), o) for o in plain)


def test_the_per_teacher_option_source_follows_the_course_shape() -> None:
    from core.services.timetable_autoplace import (
        availability_section_options,
        generate_course_meeting_options,
    )

    def options_for(availability, ids, code: str, credit: int, course_options):
        return availability_section_options(
            availability,
            ids,
            code,
            credit,
            [],
            [],
            [],
            course_key=code,
            meeting_pattern="",
            course_options=course_options,
        )

    # A meeting-policy (ENG intensive) course keeps its legal WHOLE bundles.
    eng = generate_course_meeting_options(
        "ENG101", 0, [], [], [], course_key="ENG101", meeting_pattern=""
    )
    assert len(eng) == 2
    legal = options_for(NO_MORNINGS, (7,), "ENG101", 0, eng)
    assert len(legal) == 1 and legal[0][0]["start"] == "13:00"
    assert options_for(NO_MORNINGS, (9,), "ENG101", 0, eng) == eng  # control: no rule
    # An ordinary course runs the generator with the rules.
    plain = generate_course_meeting_options(
        "CS101", 3, [], [], [], course_key="CS101", meeting_pattern=""
    )
    assert MIXED_WEEK in _weeks(options_for(SUN_PM_AND_TUE_LATE_MORNING, (7,), "CS101", 3, plain))
    assert options_for(EMPTY_AVAILABILITY, (7,), "CS101", 3, plain) == plain


# ── C.15: the teacher choice, on slots carrying the rules ─────────────

from dataclasses import replace  # noqa: E402

from core.services import section_teacher_assignment as sta  # noqa: E402

AM_SUN = (SUN, 9 * 60, 10 * 60 + 15)
OPT_SUN = (AM_SUN,)
OPT_MON = ((MON, 9 * 60, 10 * 60 + 15),)


@pytest.fixture
def pure(settings):
    """Clash, the daily cap and one teacher per section, as the ledger reads them."""
    for name, value in ots.FLAG_ON.items():
        setattr(settings, name, value)
    return settings


def _placed(course: str = "C101", eligible=(1, 2), **extra) -> sta.Slot:
    return replace(ots.course(course, [{"meetings": [AM_SUN]}], eligible=eligible)[0], **extra)


def _pending(course: str, options, eligible=(1,), **extra) -> sta.Slot:
    return replace(ots.course(course, [{"options": options}], eligible=eligible)[0], **extra)


@pytest.mark.parametrize("solver", ["greedy", "exact"])
def test_a_teacher_is_never_newly_picked_for_a_placed_section_his_rules_forbid(
    pure, solver
) -> None:
    pick = sta.greedy_assign if solver == "greedy" else sta.solve_layout_aware
    plain = _placed()
    ruled = _placed(blocked_teachers=(1,))

    assert pick([plain]).teacher[plain.key] == 1  # control: the lower id wins
    assert pick([ruled]).teacher[ruled.key] == 2
    assert sta._Ledger().blocker(ruled, 1) == sta.AVAILABILITY_REASON


@pytest.mark.parametrize("solver", ["greedy", "exact"])
def test_a_section_only_a_blocked_teacher_could_take_is_tbd_for_availability(pure, solver) -> None:
    pick = sta.greedy_assign if solver == "greedy" else sta.solve_layout_aware
    alone = _placed(eligible=(1,), blocked_teachers=(1,))

    a = pick([alone])

    assert a.status[alone.key] == sta.STATUS_TBD
    assert a.tbd_reason[alone.key] == sta.AVAILABILITY_REASON
    # Control: without the rule he takes it.
    assert pick([_placed(eligible=(1,))]).teacher[alone.key] == 1


def test_a_section_not_placed_yet_keeps_a_legal_option_free(pure) -> None:
    pending = _pending("P101", [OPT_SUN, OPT_MON])
    legal_monday = replace(pending, teacher_options=((1, (OPT_MON,)),))
    ledger = sta._Ledger()
    ledger.add(ots.pinned_load(1, 1, meetings=[OPT_MON[0]]), 1)  # teacher 1 busy on MON 09:00

    assert ledger.free_option(pending, 1) is None  # control: SUN 09:00 is still free
    assert ledger.free_option(legal_monday, 1) == "clash"
    assert (
        ledger.free_option(replace(pending, teacher_options=((1, ()),)), 1)
        == sta.AVAILABILITY_REASON
    )
    # Another teacher is not bound by teacher 1's rules.
    assert legal_monday.options_for(2) == pending.options


@pytest.mark.parametrize("clash_and_cap", [True, False])
@pytest.mark.parametrize("solver", ["greedy", "exact"])
def test_a_teacher_left_no_legal_option_is_not_given_a_missing_section(
    pure, solver, clash_and_cap
) -> None:
    pure.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = clash_and_cap
    pure.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = clash_and_cap
    pick = sta.greedy_assign if solver == "greedy" else sta.solve_layout_aware
    no_week = _pending("P101", [OPT_SUN, OPT_MON], eligible=(1, 2), teacher_options=((1, ()),))
    alone = replace(no_week, eligible=(1,))

    assert pick([no_week]).teacher[no_week.key] == 2
    assert pick([replace(no_week, teacher_options=())]).teacher[no_week.key] == 1  # control
    assert pick([alone]).tbd_reason[alone.key] == sta.AVAILABILITY_REASON


def test_the_kept_rule_keeps_a_charged_violator_with_a_legal_week(pure) -> None:
    plain = _placed()
    violating = _placed(blocked_teachers=(1,))
    no_week = _placed(blocked_teachers=(1,), teacher_options=((1, ()),))

    assert sta._violating_keys([violating], {violating.key: 1}) == {violating.key}
    assert sta._violating_keys([plain], {plain.key: 1}) == set()  # control
    assert sta._violating_keys([no_week], {no_week.key: 1}) == set()
    kept = sta._violating_keys([violating], {violating.key: 1})
    assert (
        sta.solve_layout_aware([violating], current={violating.key: 1}, kept=kept).teacher[
            violating.key
        ]
        == 1
    )
    released = sta._violating_keys([no_week], {no_week.key: 1})
    assert (
        sta.solve_layout_aware([no_week], current={no_week.key: 1}, kept=released).teacher[
            no_week.key
        ]
        == 2
    )


def test_a_missing_section_whose_teacher_has_no_legal_option_is_released(pure) -> None:
    busy = ots.pinned_load(1, 1, meetings=[AM_SUN])
    pending = _pending("P101", [OPT_SUN], eligible=(1, 2))

    # Control: left no option by a clash, the section is kept with its teacher.
    assert sta._unplaceable_keys([busy, pending], {pending.key: 1}) == {pending.key}
    no_week = replace(pending, teacher_options=((1, ()),))
    assert sta._unplaceable_keys([busy, no_week], {no_week.key: 1}) == set()


def test_only_an_engine_read_releases_a_stored_teacher_without_a_legal_week(pure) -> None:
    slot = _placed()
    info = {"engine_rows": {slot.key: 1}}
    no_week = replace(slot, blocked_teachers=(1,), teacher_options=((1, ()),))
    violating = replace(slot, blocked_teachers=(1,))

    assert sta._validated_rows([no_week], info, release_unteachable=True)[0] == {}
    assert sta._validated_rows([no_week], info)[0] == {slot.key: 1}  # a display read keeps it
    assert sta._validated_rows([violating], info, release_unteachable=True)[0] == {slot.key: 1}
    assert sta._unteachable_rows([no_week], info) == {slot.key}
    assert sta._unteachable_rows([violating], info) == frozenset()


def _guard_board(*, teacher_options=()):
    """Teacher 1 holds a missing section P (options SUN or MON 09:00) and K."""
    p = _pending("P101", [OPT_SUN, OPT_MON], teacher_options=teacher_options)
    k = _pending("K101", [OPT_MON])
    return sta.greedy_assign([p, k]), p, k


def _option(day: str, start: str, end: str) -> list[dict]:
    return [{"day": day, "start": start, "end": end}]


def test_the_placer_guard_keeps_a_pending_section_a_legal_option(pure) -> None:
    ruled, p, k = _guard_board(teacher_options=((1, (OPT_MON,)),))
    assert ruled.teacher[p.key] == ruled.teacher[k.key] == 1
    monday = _option("MON", "09:00", "10:15")

    check = sta.PlacerGuard(ruled).check(k.pr4_key, {1}, {})
    assert check(monday) is True  # P's only legal option would go
    assert check(_option("TUE", "09:00", "10:15")) is False
    # Control: without the rule P could still use Sunday.
    plain, _p, _k = _guard_board()
    assert sta.PlacerGuard(plain).check(k.pr4_key, {1}, {})(monday) is False


@pytest.mark.parametrize("clash_and_cap", [True, False])
def test_the_placer_guard_ranks_last_a_time_no_open_teacher_may_teach(pure, clash_and_cap) -> None:
    pure.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = clash_and_cap
    pure.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = clash_and_cap
    tbd = _placed("T101", eligible=(7,), blocked_teachers=(7,))
    a = sta.greedy_assign([tbd])
    assert a.status[tbd.key] == sta.STATUS_TBD
    morning, afternoon = _option("SUN", "09:00", "10:15"), _option("SUN", "13:00", "14:15")

    check = sta.PlacerGuard(a, NO_MORNINGS).check(tbd.pr4_key, set(), {})
    assert check(morning) is True
    assert check(afternoon) is False
    # Controls: no availability (nothing at stake without clash and cap), or an
    # online section.
    plain = sta.PlacerGuard(a).check(tbd.pr4_key, set(), {})
    assert plain is None if not clash_and_cap else plain(morning) is False
    exempt = sta.PlacerGuard(a, NO_MORNINGS).check(tbd.pr4_key, set(), {}, availability_exempt=True)
    assert exempt(morning) is False


def _charged(slot: sta.Slot, teacher: int) -> sta.TeacherAssignment:
    return sta.TeacherAssignment(
        teacher={slot.key: teacher},
        status={slot.key: sta.STATUS_ENGINE},
        tbd_reason={},
        kept=frozenset(),
        universe=frozenset({1, 2}),
        slots=(slot,),
    )


def test_a_re_pick_that_charges_a_blocked_teacher_adds_a_violation(pure) -> None:
    ruled = _placed(blocked_teachers=(1,))

    assert sta.violations(_charged(ruled, 1))["availability"] == 1
    assert sta.adds_violations(_charged(ruled, 1), _charged(ruled, 2))
    assert not sta.adds_violations(_charged(ruled, 2), _charged(ruled, 1))
    plain = _placed()  # control: no rule, no key, nothing added
    assert "availability" not in sta.violations(_charged(plain, 1))
    assert not sta.adds_violations(_charged(plain, 1), _charged(plain, 2))


def test_the_availability_reason_key_appears_only_while_rules_bind_a_slot(pure) -> None:
    ruled = sta.coverage_of(sta.greedy_assign([_placed(eligible=(1,), blocked_teachers=(1,))]))
    plain = sta.coverage_of(sta.greedy_assign([_placed(eligible=(1,))]))

    assert ruled["tbd_reasons"][sta.AVAILABILITY_REASON] == 1
    assert ruled["violations"]["availability"] == 0
    assert list(plain["tbd_reasons"]) == ["d10", "weekly_ceiling", "clash", "daily_cap", "bound"]
    assert "availability" not in plain["violations"]


# ── C.15 end to end: _collect writes each ruled teacher's rules into the slots ──


def _ruled_board(
    *, eligible=("Dr A", "Dr B"), online: bool = False, planned: int = 1, placed: bool = True
):
    sc, board = ots.scenario("rules", programs=("AI",))
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="CS101",
        course_name="INTRO",
        programme_term=2,
        credit_hours=3,
        is_online=online,
    )
    from core.services.course_identity import planner_course_key

    key = planner_course_key("CS101", "INTRO")
    ots.budget(sc, key, planned)
    people = [ots.instructor(name) for name in eligible]
    for index, person in enumerate(people):  # one primary link per programme
        ots.link("AI", "CS101", person, role="co" if index else "primary")
    ts = None
    if placed:
        ts = ots.section(sc, key, "S1")
        ots.place(board, ts, [("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15")])
    return sc, board, key, ts, people


def _engine_row(sc, key: str, label: str, person) -> None:
    from core.models import SectionInstructor

    SectionInstructor.objects.create(
        scenario=sc,
        course_key=key,
        section=label,
        instructor=person,
        role=sta.ROLE_PRIMARY,
        source=sta.SOURCE_ENGINE,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False])
def test_the_closure_never_picks_a_teacher_at_a_time_his_rules_forbid(av_on, flag) -> None:
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _board, key, ts, (a, b) = _ruled_board()
    rule(a, KIND_UNAVAILABLE, end="13:00")  # Dr A: no mornings

    shown = sta.section_teacher_display(sc.id)[ts.pk]
    engine = sta.load_assignment(sc.id)

    assert shown["id"] == (b.pk if flag else a.pk)
    assert engine.teacher[f"{key}_S1"] == (b.pk if flag else a.pk)
    assert engine.slot(f"{key}_S1").blocked_teachers == ((a.pk,) if flag else ())


@pytest.mark.django_db
def test_a_section_only_a_blocked_teacher_may_teach_reads_tbd_for_availability(av_on) -> None:
    sc, _board, key, ts, (a,) = _ruled_board(eligible=("Dr A",))
    rule(a, KIND_UNAVAILABLE, end="13:00")

    shown = sta.section_teacher_display(sc.id)[ts.pk]
    coverage = sta.coverage_of(sta.load_assignment(sc.id))

    assert (shown["status"], shown["reason"]) == (sta.STATUS_TBD, sta.AVAILABILITY_REASON)
    assert coverage["tbd_reasons"][sta.AVAILABILITY_REASON] == 1


@pytest.mark.django_db
def test_an_online_section_is_not_bound_by_its_teachers_rules(av_on) -> None:
    sc, _board, _key, ts, (a, _b) = _ruled_board(online=True)
    rule(a, KIND_UNAVAILABLE, end="13:00")

    assert sta.section_teacher_display(sc.id)[ts.pk]["id"] == a.pk


def _two_boards(name: str, *, ds_online: bool, budgeted: bool = True):
    """GS101 (2 x 75) on two term-2 boards: online on the AI board, online on the
    DS board too or a campus course there (``ds_online``); one planned section when
    ``budgeted``. Dr A and Dr B may teach it on both programmes."""
    from core.services.course_identity import planner_course_key

    sc, ai_board = ots.scenario(name, programs=("AI", "DS"), board_program="AI")
    ds_board = DeliveryBoard.objects.create(
        scenario=sc, label="Term 2 DS", nominal_term=2, program="DS"
    )
    for program, online in (("AI", True), ("DS", ds_online)):
        ProgrammeRequirement.objects.create(
            program=program,
            course_code="GS101",
            course_name="INTRO",
            programme_term=2,
            credit_hours=3,
            is_online=online,
        )
    key = planner_course_key("GS101", "INTRO")
    if budgeted:
        ots.budget(sc, key, 1, code="GS101")
    people = [_person("Dr A"), _person("Dr B")]
    for program in ("AI", "DS"):
        for index, person in enumerate(people):
            ots.link(program, "GS101", person, role="co" if index else "primary")
    return sc, ai_board, ds_board, key, people


@pytest.mark.django_db
@pytest.mark.parametrize("budgeted", [True, False], ids=["budgeted", "own-section"])
@pytest.mark.parametrize("ai_only", [False, True], ids=["placed-on-both", "placed-on-ai-only"])
@pytest.mark.parametrize("ds_online", [True, False], ids=["online-everywhere", "campus-on-ds"])
def test_the_teacher_choice_judges_the_online_exemption_on_every_board(
    av_on, budgeted, ai_only, ds_online
) -> None:
    """D-A in the teacher choice (identity:F3, foundation:F1): a placed section is
    exempt iff every placed copy is online AND, when its course is budgeted, the
    course is online on every board of its term. Dr A may not teach mornings and
    the section meets SUN/TUE 09:00, so a bound section goes to Dr B (and Dr A is
    blocked); an exempt one keeps the lower id, Dr A. An own section (no budget)
    is judged by its placed copies alone."""
    sc, ai_board, ds_board, key, (a, b) = _two_boards(
        f"exempt {budgeted} {ai_only} {ds_online}", ds_online=ds_online, budgeted=budgeted
    )
    ts = ots.section(sc, key, "S1")
    for board in (ai_board,) if ai_only else (ai_board, ds_board):
        ots.place(board, ts, [("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15")])
    rule(a, KIND_UNAVAILABLE, end="13:00")

    engine = sta.load_assignment(sc.id, incomplete="collect")
    slot = engine.slot(f"{key}_S1")

    exempt = ds_online or (ai_only and not budgeted)
    assert engine.teacher[f"{key}_S1"] == (a.pk if exempt else b.pk)
    assert slot.blocked_teachers == (() if exempt else (a.pk,))
    assert ((key, "S1") in helper.online_exempt_sections(sc.id)) is exempt


@pytest.mark.django_db
@pytest.mark.parametrize("windows", [True, False], ids=["windows", "no-windows"])
@pytest.mark.parametrize("ds_online", [True, False], ids=["online-everywhere", "campus-on-ds"])
def test_a_missing_section_is_exempt_only_when_online_on_every_board_of_its_term(
    av_on, windows, ds_online
) -> None:
    """D-A for a section not placed yet: exempt iff its course is online on every
    board of its term. Dr A may teach only after 17:40, so he has no campus week:
    bound, the section goes to Dr B and Dr A's legal options are none (with the
    evening windows on they are NOT legal for him: legality is judged on the
    daytime options, foundation:F1). Exempt, Dr A keeps it (the lower id)."""
    av_on.TIMETABLE_ONLINE_EVENING_WINDOWS = windows
    sc, _ai, _ds, key, (a, b) = _two_boards(f"missing {windows} {ds_online}", ds_online=ds_online)
    rule(a, KIND_UNAVAILABLE, end="17:40")

    engine = sta.load_assignment(sc.id, incomplete="collect")
    slot = engine.slot(f"{key}_S1")

    assert not slot.placed
    assert engine.teacher[f"{key}_S1"] == (a.pk if ds_online else b.pk)
    assert dict(slot.teacher_options).get(a.pk) == (None if ds_online else ())


@pytest.mark.django_db
@pytest.mark.parametrize("windows", [True, False], ids=["windows", "no-windows"])
@pytest.mark.parametrize("runner", ["run_placer", "plain"])
@pytest.mark.parametrize("ruled", [True, False], ids=["rule", "no-rule-control"])
def test_a_partly_online_course_is_placed_on_every_board_by_a_teacher_who_may_teach_it(
    av_on, windows, runner, ruled
) -> None:
    """foundation:F1: GS101 is online on the AI board and a campus course on the
    DS board; Dr A may teach only after 17:40 (no campus week). The section is
    placed on BOTH boards and taught by Dr B, with nothing left unplaced (it used
    to be charged to Dr A and left off the DS board). Control: without the rule
    Dr A (the lower id) teaches it, on both boards."""
    from core.services.timetable_autoplace import auto_place_scenario

    av_on.TIMETABLE_ONLINE_EVENING_WINDOWS = windows
    sc, ai_board, ds_board, key, (a, b) = _two_boards(
        f"f1 {windows} {runner} {ruled}", ds_online=False
    )
    if ruled:
        rule(a, KIND_UNAVAILABLE, end="17:40")

    def place():
        return auto_place_scenario(sc.id, strategy="compact")

    result = sta.run_placer(sc.id, place, rebuild=True) if runner == "run_placer" else place()

    for board in (ai_board, ds_board):
        assert len(_placed_meetings(sc, key, board=board)) == 2, board.program
    teacher = sta.load_assignment(sc.id, incomplete="collect").teacher[f"{key}_S1"]
    assert teacher == (b.pk if ruled else a.pk)
    for board_result in result["boards"].values():
        assert not (board_result.get("instructor_availability") or {}).get("unplaced")


@pytest.mark.django_db
@pytest.mark.parametrize("placed", [True, False], ids=["placed", "not-placed"])
@pytest.mark.parametrize("ruled", [True, False], ids=["rule", "no-rule-control"])
def test_a_pinned_teacher_outside_the_links_is_bound_by_his_rules(av_on, placed, ruled) -> None:
    """foundation:F4 (M27, M28, M33): a department pin of Dr P, who is not linked
    to the course, is in the rule universe and among the section's candidates:
    his "no mornings" blocks the placed SUN/TUE 09:00 section (counted as a
    violation of the pinned map) or leaves the missing section only his afternoon
    options. Control: no rule, nothing written."""
    sc, _board, key, _ts, (_a,) = _ruled_board(eligible=("Dr A",), placed=placed)
    p = ots.instructor("Dr P")
    _pin(sc, key, "S1", p)
    if ruled:
        rule(p, KIND_UNAVAILABLE, end="13:00")

    engine = sta.load_assignment(sc.id, incomplete="collect")
    slot = engine.slot(f"{key}_S1")

    assert slot.fixed == p.pk and p.pk not in slot.eligible
    if placed:
        assert slot.blocked_teachers == ((p.pk,) if ruled else ())
        assert sta.violations(engine).get("availability") == (1 if ruled else None)
    else:
        legal = dict(slot.teacher_options).get(p.pk)
        if ruled:
            assert legal and all(start >= 13 * 60 for option in legal for _d, start, _e in option)
        else:
            assert legal is None


def test_the_exact_solve_keeps_a_pinned_teachers_last_legal_option_free(pure) -> None:
    """foundation:F4 (M31): teacher 1 is pinned to a missing section whose only
    LEGAL option is MON 09:00; the placed choice section K at MON 09:00 would take
    it, so the exact solve gives K to teacher 2. Control: without the rule SUN
    stays free for the pinned section and K keeps the lower id."""
    pinned = ots.course("P101", [{"options": [OPT_SUN, OPT_MON], "fixed": 1}], eligible=(1,))[0]
    k = ots.course("K101", [{"meetings": [OPT_MON[0]]}], eligible=(1, 2))[0]
    ruled = replace(pinned, teacher_options=((1, (OPT_MON,)),))

    assert sta.solve_layout_aware([pinned, k]).teacher[k.key] == 1
    assert sta.solve_layout_aware([ruled, k]).teacher[k.key] == 2


@pytest.mark.django_db
def test_a_stored_violating_teacher_with_a_legal_week_is_kept(av_on) -> None:
    sc, _board, key, ts, (a, _b) = _ruled_board()
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_UNAVAILABLE, end="13:00")  # the afternoons are still legal

    assert sta.load_assignment(sc.id).teacher[f"{key}_S1"] == a.pk
    assert sta.section_teacher_display(sc.id)[ts.pk]["id"] == a.pk


@pytest.mark.django_db
def test_a_stored_teacher_without_a_legal_week_is_released_on_engine_reads_only(av_on) -> None:
    sc, _board, key, ts, (a, b) = _ruled_board()
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))  # one day: no 2 x 75 week at all

    # An engine phase opts in (fix plan D-H); a plain read, even in raise mode,
    # and the display keep the stored teacher.
    assert sta.load_assignment(sc.id, release_unteachable=True).teacher[f"{key}_S1"] == b.pk
    assert sta.load_assignment(sc.id).teacher[f"{key}_S1"] == a.pk
    assert sta.section_teacher_display(sc.id)[ts.pk]["id"] == a.pk
    # Inside one request scope every read keeps its own answer.
    with sta.assignment_scope(sc.id):
        assert sta.section_teacher_display(sc.id)[ts.pk]["id"] == a.pk
        assert sta.load_assignment(sc.id, release_unteachable=True).teacher[f"{key}_S1"] == b.pk
        assert sta.section_teacher_display(sc.id)[ts.pk]["id"] == a.pk
        assert sta.load_assignment(sc.id).teacher[f"{key}_S1"] == a.pk
        assert sta.load_assignment(sc.id, release_unteachable=True).teacher[f"{key}_S1"] == b.pk


def _meeting_names(ts) -> set[str]:
    from core.models import TermSectionMeeting

    return set(
        TermSectionMeeting.objects.filter(term_section=ts).values_list("instructor", flat=True)
    )


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["reconcile", "apply_primary_instructor"])
def test_a_non_engine_write_keeps_the_stored_teacher_without_a_legal_week(av_on, path) -> None:
    """D-H (foundation:F3): only an engine phase releases a stored engine row
    whose teacher has no legal week. The staff "reconcile" action and
    ``apply_primary_instructor`` without a passed map write the STORED teacher's
    name, the one the row and the workspace show. Control: an engine read
    (``release_unteachable=True``) re-picks Dr B."""
    from core.services.course_instructor_assignment import (
        apply_primary_instructor,
        reconcile_scenario_instructors,
    )

    sc, board, key, ts, (a, b) = _ruled_board()
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))  # no 2 x 75 week at all

    if path == "reconcile":
        reconcile_scenario_instructors(sc)
    else:
        apply_primary_instructor(ts, sc, board, ts.course_code)

    assert _meeting_names(ts) == {"Dr A"}
    assert sta.section_teacher_display(sc.id)[ts.pk]["id"] == a.pk
    assert sta.load_assignment(sc.id, release_unteachable=True).teacher[f"{key}_S1"] == b.pk


@pytest.mark.django_db
def test_the_scenario_quality_counts_the_stored_teacher_without_a_legal_week(av_on) -> None:
    """D-H (foundation:F3): ``build_scenario_quality`` never releases: the stored
    Dr A (no legal week, nobody else may teach it) still counts as assigned and
    his days are counted, as the workspace shows it. Control: an engine read
    leaves the section TBD for availability."""
    from core.services.timetable_scenario_quality import build_scenario_quality

    sc, _board, key, ts, (a,) = _ruled_board(eligible=("Dr A",))
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    instructor = build_scenario_quality(sc.id)["instructor"]

    assert (instructor["coverage"]["assigned"], instructor["coverage"]["tbd"]) == (1, 0)
    assert instructor["working_days"] > 0
    engine = sta.load_assignment(sc.id, release_unteachable=True)
    assert engine.tbd_reason[f"{key}_S1"] == sta.AVAILABILITY_REASON


@pytest.mark.django_db
@pytest.mark.parametrize("ruled", [True, False], ids=["rule", "no-rule-control"])
def test_a_place_missing_run_places_under_the_released_map(av_on, ruled) -> None:
    """D-H: ``run_placer``'s place-missing map is an engine read, so the missing
    section's stored Dr A (no legal week) is released before the greedy runs and
    Dr B's section is placed; the final ensure then stores Dr B. Control: no
    rule, Dr A keeps it."""
    from core.models import SectionInstructor
    from core.services.timetable_autoplace import auto_place_scenario

    sc, _board, key, _ts, (a, b) = _ruled_board(placed=False)
    _engine_row(sc, key, "S1", a)
    if ruled:
        rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    sta.run_placer(sc.id, lambda: auto_place_scenario(sc.id, strategy="compact"), rebuild=False)

    assert len(_placed_meetings(sc, key)) == 2
    stored = SectionInstructor.objects.filter(scenario=sc, course_key=key, section="S1")
    assert list(stored.values_list("instructor_id", flat=True)) == [b.pk if ruled else a.pk]


@pytest.mark.django_db
def test_the_released_map_is_memoised_without_a_prior_display_read(av_on) -> None:
    """D-H (foundation:F5): inside one scope, a second engine read of a map that
    released a stored row is served from the memo (only the layout fingerprint
    is read again), even when no display read came first."""
    sc, _board, key, _ts, (a, b) = _ruled_board()
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    with sta.assignment_scope(sc.id):
        first = sta.load_assignment(sc.id, release_unteachable=True)
        with CaptureQueriesContext(connection) as ctx:
            second = sta.load_assignment(sc.id, release_unteachable=True)

    assert second is first and first.teacher[f"{key}_S1"] == b.pk
    assert len(ctx.captured_queries) == 1, [q["sql"] for q in ctx.captured_queries]


@pytest.mark.django_db
def test_a_manual_create_leaves_every_other_stored_teacher_row_as_it_was(av_on) -> None:
    from core.models import SectionInstructor

    sc, board, key, _ts, (a, b) = _ruled_board(planned=2)
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    def rows():
        return sorted(
            SectionInstructor.objects.filter(scenario=sc).values_list(
                "pk", "course_key", "section", "instructor_id", "role", "source"
            )
        )

    before = rows()
    new = ots.section(sc, key, "S2")
    ots.place(board, new, [("MON", "13:00", "14:15"), ("WED", "13:00", "14:15")])
    sta.name_new_section(sc.id, new, board)

    after = rows()
    assert [row for row in after if row[2] == "S1"] == before
    assert [row[3] for row in after if row[2] == "S2"] == [b.pk]  # A has no legal week
    # Control: an engine ensure (a place-missing run) does release the row.
    sta.prepare_section_teachers(sc.id, phase="ensure", release_unteachable=True)
    assert [row[3] for row in rows() if row[2] == "S1"] != [a.pk]


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False])
def test_a_missing_section_gets_a_teacher_with_a_legal_week_without_clash_or_cap(
    av_on, flag
) -> None:
    """Neither clash nor the daily cap would give a missing section options;
    the rules still do, so its teacher is one who can teach it."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    av_on.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = False
    av_on.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = False
    sc, _board, key, _ts, (a, b) = _ruled_board(placed=False)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    engine = sta.load_assignment(sc.id)
    missing = engine.slot(f"{key}_S1")

    assert not missing.placed
    assert engine.teacher[f"{key}_S1"] == (b.pk if flag else a.pk)
    assert bool(missing.options) is flag


# ── T-P2: nothing reads the rules unless the flag AND one teacher are on ──


def _p1_steps(scenario):
    from core.models import SectionPlacement
    from core.services.course_instructor_assignment import (
        apply_primary_instructor,
        reconcile_scenario_instructors,
    )
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_board_persistence import restore_scenario, snapshot_scenario
    from core.services.timetable_export import export_scenario_xlsx
    from core.services.timetable_graph_twin import build_scenario_graph_payload
    from core.services.timetable_instructor_backstop import scenario_instructor_clash_report
    from core.services.timetable_required_sections import reinstate_missing_required_sections
    from core.services.timetable_scenario_quality import build_scenario_quality
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from core.services.timetable_workspace import (
        compute_scenario_safety_summary,
        detect_board_conflicts,
        preview_placement_slot_candidates,
        validate_placement,
    )
    from core.timetable_workspace_views import _placement_to_dict

    board = DeliveryBoard.objects.get(scenario=scenario, label="Term 2")

    def placement():
        return (
            SectionPlacement.objects.filter(board=board)
            .select_related("term_section")
            .order_by("id")
            .first()
        )

    return {
        "auto_place_scenario": lambda: auto_place_scenario(scenario.id, strategy="compact"),
        "reinstate": lambda: reinstate_missing_required_sections(scenario.id),
        "apply_primary_instructor": lambda: apply_primary_instructor(
            placement().term_section, scenario, board, placement().term_section.course_code
        ),
        "reconcile": lambda: reconcile_scenario_instructors(scenario),
        "snapshot_restore": lambda: restore_scenario(scenario.id, snapshot_scenario(scenario.id)),
        "safety_summary": lambda: compute_scenario_safety_summary(scenario.id),
        "detect_board_conflicts": lambda: detect_board_conflicts(board.id),
        "quality": lambda: build_scenario_quality(scenario.id),
        "backstop": lambda: scenario_instructor_clash_report(scenario.id),
        "placement_to_dict": lambda: _placement_to_dict(placement()),
        "validate_placement": lambda: validate_placement(
            board.id,
            "SUN",
            "08:00",
            "09:15",
            "",
            placement().term_section_id,
            exclude_placement_id=placement().id,
        ),
        "preview_placement": lambda: preview_placement_slot_candidates(placement().id),
        "graph_twin": lambda: build_scenario_graph_payload(scenario.id),
        "export": lambda: export_scenario_xlsx(scenario.id),
        "runner_current": lambda: run_v2_optimisation_guarded(
            scenario.id, mode="current", max_iterations=2, run_chain=False, run_cpsat=False
        ),
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("flag", "one_teacher"), [(True, False), (False, True), (True, True)], ids=str
)
def test_the_rules_are_read_only_with_the_flag_and_one_teacher_on(av_on, flag, one_teacher) -> None:
    from tests.test_one_teacher_flag_parity import LINKS, build_corpus

    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    av_on.TIMETABLE_ONE_TEACHER_PER_SECTION = one_teacher
    scenario = build_corpus()
    for person in Instructor.objects.filter(full_name__in=set(LINKS.values())):
        rule(person, KIND_UNAVAILABLE, ("THU",), "16:00", "17:00")

    reads = {}
    for name, step in _p1_steps(scenario).items():
        with CaptureQueriesContext(connection) as ctx:
            step()
        reads[name] = len(rule_queries(ctx.captured_queries))

    if flag and one_teacher:  # control: the rules ARE read on these paths
        assert reads["auto_place_scenario"] >= 1 and reads["runner_current"] >= 1
    else:
        assert reads == {name: 0 for name in reads}


def test_a_teacher_without_a_legal_week_is_not_an_open_teacher_of_the_guard(pure) -> None:
    no_week = _placed("T101", eligible=(7,), blocked_teachers=(7,), teacher_options=((7, ()),))
    legal_week = _placed("T101", eligible=(7,), blocked_teachers=(7,))

    assert no_week.pr4_key not in sta.PlacerGuard(sta.greedy_assign([no_week])).open
    assert sta.PlacerGuard(sta.greedy_assign([legal_week])).open == {legal_week.pr4_key: (7,)}


@pytest.mark.django_db
def test_a_place_missing_run_stores_the_map_it_placed_under(av_on) -> None:
    from core.models import SectionInstructor

    sc, _board, key, _ts, (a, b) = _ruled_board()
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    sta.run_placer(sc.id, dict, rebuild=False)

    stored = SectionInstructor.objects.filter(scenario=sc, course_key=key, section="S1")
    assert list(stored.values_list("instructor_id", flat=True)) == [b.pk]


@pytest.mark.django_db
def test_one_request_reads_the_rules_once(av_on) -> None:
    sc, _board, _key, ts, (a, _b) = _ruled_board()
    rule(a, KIND_UNAVAILABLE, end="13:00")

    from core.models import SectionPlacement

    def read_drag_read() -> None:
        """Two collects: the drag changes the layout between the reads."""
        sta.section_teacher_display(sc.id)
        moved = SectionPlacement.objects.filter(term_section=ts, day="TUE")
        start = "13:00" if moved.filter(start_time="09:00").exists() else "09:00"
        moved.update(start_time=start, end_time="14:15" if start == "13:00" else "10:15")
        sta.load_assignment(sc.id)

    for scope in (sta.request_scope, lambda: sta.assignment_scope(sc.id)):
        with CaptureQueriesContext(connection) as ctx:
            with scope():
                read_drag_read()
        assert len(rule_queries(ctx.captured_queries)) == 1
    # Control: unscoped, each collect loads.
    with CaptureQueriesContext(connection) as ctx:
        read_drag_read()
    assert len(rule_queries(ctx.captured_queries)) == 2
    assert sta.section_teacher_display(sc.id)[ts.pk]["id"] != a.pk


def test_fixed_section_keys_are_the_pinned_and_frozen_sections(pure) -> None:
    pinned = ots.course("PIN101", [{"meetings": [AM_SUN], "fixed": 1}], eligible=(1,))[0]
    frozen = replace(
        ots.course("FRZ101", [{"meetings": [AM_SUN], "fixed": 2}], eligible=(2,))[0],
        fixed_kind=sta.STATUS_FROZEN,
    )
    choice = _placed("CHO101", eligible=(3,))

    a = sta.greedy_assign([pinned, frozen, choice])

    assert a.status[choice.key] == sta.STATUS_ENGINE
    assert helper.fixed_section_keys(a) == {pinned.pr4_key, frozen.pr4_key}


# ── C.2 / C.3: the greedy placer (feed, filter, fixed rank-last, report) ──
#
# One budgeted 3-credit course (2 x 75) on one board; the teacher's rules decide
# where its section may go. Controls: the flag off, the feed taken out, or an
# ENGINE map pinned where the closure would otherwise not charge the teacher.

GREEDY_STRATEGIES = ("compact", "morning", "balanced")
GREEDY_SEEDS = (3, 11, 29, None)


def _person(name: str) -> Instructor:
    return Instructor.objects.filter(full_name=name).first() or ots.instructor(name)


def _greedy_scenario(
    name: str,
    *,
    teachers=("Dr A",),
    code: str = "CS101",
    credit: int = 3,
    online: bool = False,
    pin: bool = False,
    programs=("AI",),
):
    """``(scenario, board, course_key, people)``: one budgeted section S1, not
    placed, of a course every teacher in ``teachers`` may teach (``pin``: the
    first one is the section's pinned teacher)."""
    from core.services.course_identity import planner_course_key

    sc, board = ots.scenario(name, programs=programs, board_program=programs[0])
    ProgrammeRequirement.objects.create(
        program=programs[0],
        course_code=code,
        course_name="INTRO",
        programme_term=2,
        credit_hours=credit,
        is_online=online,
    )
    key = planner_course_key(code, "INTRO")
    ots.budget(sc, key, 1, code=code, credit=credit)
    people = [_person(full_name) for full_name in teachers]
    for index, person in enumerate(people):
        ots.link(programs[0], code, person, role="co" if index else "primary")
    if pin:
        _pin(sc, key, "S1", people[0])
    return sc, board, key, people


def _pin(sc, key: str, label: str, person) -> None:
    from core.models import SectionInstructor

    SectionInstructor.objects.create(
        scenario=sc,
        course_key=key,
        section=label,
        instructor=person,
        role=sta.ROLE_PRIMARY,
        source=sta.SOURCE_DEPARTMENT,
    )


def _greedy(sc, strategy: str = "compact", seed: int | None = None, **kwargs) -> dict:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_run_seed import seeded_timetable_run

    with seeded_timetable_run(seed):
        return auto_place_scenario(sc.id, strategy=strategy, **kwargs)


def _placed_meetings(sc, key: str, label: str = "S1", board=None) -> list[tuple[str, str, str]]:
    from core.models import SectionPlacement

    rows = SectionPlacement.objects.filter(
        board__scenario=sc, term_section__course_key=key, term_section__section=label
    )
    if board is not None:
        rows = rows.filter(board=board)
    return sorted(rows.values_list("day", "start_time", "end_time"))


def _blocked(meetings, person, av_rules) -> list[tuple[str, str, str]]:
    """The meetings ``person``'s rules (``av_rules``, compiled) forbid."""
    return [
        meeting
        for meeting in meetings
        if helper.option_blocked(
            av_rules, (person.pk,), [{"day": meeting[0], "start": meeting[1], "end": meeting[2]}]
        )
    ]


def _rules_of(person):
    from core.services.instructor_availability import load_instructor_availability

    return load_instructor_availability([person.pk])


def _board_result(result: dict) -> dict:
    boards = result.get("boards") or {}
    assert len(boards) == 1, boards
    return next(iter(boards.values()))


def _engine_map(sc, key: str, person):
    """The scenario's map with ``key`` charged to ``person`` as an ENGINE pick: a
    run that pinned it (``pinned_assignment``) places under exactly this map."""
    current = sta.load_assignment(sc.id)
    v2 = f"{key}_S1"
    return replace(
        current,
        teacher={**current.teacher, v2: person.pk},
        status={**current.status, v2: sta.STATUS_ENGINE},
        tbd_reason={k: v for k, v in current.tbd_reason.items() if k != v2},
    )


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_the_greedy_never_places_an_engine_teachers_section_in_a_forbidden_cell(
    av_on, flag
) -> None:
    """T-G4: every strategy and seed keeps a "no mornings" teacher's section in the
    afternoon; with the flag off the same board lands in a morning for some run."""
    from core.services.timetable_board_persistence import reset_scenario

    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _board, key, (a,) = _greedy_scenario("t-g4")
    rule(a, KIND_UNAVAILABLE, end="13:00")
    rules = _rules_of(a)

    blocked = []
    for strategy in GREEDY_STRATEGIES:
        for seed in GREEDY_SEEDS:
            reset_scenario(sc.id, keep_locked=False)
            _greedy(sc, strategy, seed)
            meetings = _placed_meetings(sc, key)
            assert len(meetings) == 2, (strategy, seed, meetings)
            blocked += _blocked(meetings, a, rules)
    assert bool(blocked) is not flag, blocked


def _mixed_only(person) -> None:
    """``person`` may teach only SUN 13:00-17:15 and TUE 09:00-12:05: no lecture
    period is open on both days, so every week of the course list breaks the
    rule and only a mixed-period week (the feed's) is legal."""
    rule(person, KIND_AVAILABLE_ONLY, ("SUN",), "13:00", "17:15")
    rule(person, KIND_AVAILABLE_ONLY, ("TUE",), "09:00", "12:05")


@pytest.mark.django_db
def test_the_greedy_feed_places_an_engine_teachers_section_in_a_mixed_period_week(
    av_on,
) -> None:
    """T-G2: the feed generates the teacher's mixed-period weeks, so the section
    is placed legally (one meeting SUN afternoon, one TUE morning)."""
    sc, _board, key, (a,) = _greedy_scenario("t-g2")
    _mixed_only(a)

    result = _greedy(sc)

    meetings = _placed_meetings(sc, key)
    assert [day for day, _s, _e in meetings] == ["SUN", "TUE"], meetings
    assert not _blocked(meetings, a, _rules_of(a))
    assert _board_result(result)["instructor_availability"] == {"ranked_last": [], "unplaced": []}


@pytest.mark.django_db
def test_without_the_feed_the_same_section_is_left_unplaced_by_the_rule(av_on, monkeypatch) -> None:
    """T-G2's control: the same engine map, with the greedy's feed reduced to the
    course list and its complete legal set (tier 2, fix plan D-C) taken out,
    leaves the section unplaced with ``INSTRUCTOR_UNAVAILABLE``."""
    from core.services import timetable_autoplace as autoplace
    from core.services.timetable_decision_trace import INSTRUCTOR_UNAVAILABLE

    sc, _board, key, (a,) = _greedy_scenario("t-g2 control")
    _mixed_only(a)
    engine = sta.load_assignment(sc.id)
    assert engine.teacher[f"{key}_S1"] == a.pk  # a legal (mixed) week exists for him
    monkeypatch.setattr(
        autoplace,
        "availability_section_options",
        lambda *args, course_options, **kwargs: course_options,
    )
    monkeypatch.setattr(autoplace, "availability_complete_options", lambda *a, **k: [])

    with sta.pinned_assignment(sc.id, engine):
        result = _greedy(sc)

    assert _placed_meetings(sc, key) == []
    board = _board_result(result)
    (failure,) = board["room_failures"]
    assert failure["reason"] == INSTRUCTOR_UNAVAILABLE
    assert failure["context"]["instructor_id"] == a.pk
    assert board["instructor_availability"]["unplaced"] == [
        {"section": f"{key}|S1", "instructor_ids": [a.pk], "binding": True}
    ]


@pytest.mark.django_db
def test_the_placer_guard_and_the_greedy_ask_for_the_same_legal_options(av_on, monkeypatch) -> None:
    """T-G3 (as fix plan D-C restates it): the one-teacher guard
    (``_Static.teacher_options``) holds the COMPLETE legal set of the very
    arguments the greedy's per-teacher feed is asked with, so it holds every
    option of that feed and every week the greedy's tier 2 may take."""
    import sys

    from core.services import timetable_autoplace as autoplace

    sc, _board, key, (a,) = _greedy_scenario("t-g3")
    _mixed_only(a)
    real = autoplace.availability_section_options
    seen: dict[str, list] = {}

    def spy(*args, **kwargs):
        out = real(*args, **kwargs)
        seen.setdefault(sys._getframe(1).f_code.co_name, []).append((args, kwargs, out))
        return out

    monkeypatch.setattr(autoplace, "availability_section_options", spy)
    _greedy(sc)

    ((args, kwargs, greedy),) = seen["auto_place_board"]
    static = sta._load_static(sc.id)
    guard = static.teacher_options(key, static.budgets[key], a.pk, helper.availability_for(sc.id))
    complete = sta._option_cells(autoplace.availability_complete_options(*args, **kwargs))
    assert greedy and set(sta._option_cells(greedy)) < set(guard)
    assert guard == complete


@pytest.mark.django_db
@pytest.mark.parametrize("pinned", [True, False], ids=["fixed", "engine-control"])
def test_a_fixed_teacher_without_a_legal_week_is_placed_and_reported(av_on, pinned) -> None:
    """T-G5: "only Thursday" leaves a 2 x 75 course no week. A pinned teacher's
    section is placed anyway, at an option with as few forbidden meetings as
    possible, and reported; the same teacher
    charged as an ENGINE pick (pinned map) leaves it unplaced."""
    from core.services.timetable_decision_trace import INSTRUCTOR_UNAVAILABLE

    sc, _board, key, (a,) = _greedy_scenario("t-g5", pin=pinned)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    if pinned:
        result = _greedy(sc)
    else:
        with sta.pinned_assignment(sc.id, _engine_map(sc, key, a)):
            result = _greedy(sc)

    report = _board_result(result)["instructor_availability"]
    meetings = _placed_meetings(sc, key)
    if pinned:
        # Placed at the best option that breaks the rule the fewest times: one
        # meeting on Thursday, one not (any 2 x 75 week needs two days).
        assert len(meetings) == 2
        assert len(_blocked(meetings, a, _rules_of(a))) == 1, meetings
        assert report == {
            "ranked_last": [
                {"section": f"{key}|S1", "instructor_ids": [a.pk], "reason": "rule_cannot_be_met"}
            ],
            "unplaced": [],
        }
    else:
        assert meetings == []
        assert _board_result(result)["room_failures"][0]["reason"] == INSTRUCTOR_UNAVAILABLE
        assert report["ranked_last"] == []
        assert [row["section"] for row in report["unplaced"]] == [f"{key}|S1"]


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_a_fixed_teacher_takes_a_legal_option_over_a_better_scored_forbidden_one(
    av_on, flag
) -> None:
    """T-G6: the "morning" strategy scores the mornings best; a pinned "no
    mornings" teacher's section still goes to the afternoon (control: flag off,
    the morning wins)."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _board, key, (a,) = _greedy_scenario("t-g6", pin=True)
    rule(a, KIND_UNAVAILABLE, end="13:00")

    _greedy(sc, "morning")

    meetings = _placed_meetings(sc, key)
    assert len(meetings) == 2
    assert bool(_blocked(meetings, a, _rules_of(a))) is not flag, meetings


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_a_warm_start_baseline_at_a_forbidden_time_is_not_retained(av_on, flag) -> None:
    """T-G7: decision trace and warm start on; the baseline (SUN 09:00) is free
    and scored, but its pinned teacher may not teach mornings, so it is not
    retained (control: flag off, the baseline is kept)."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    av_on.TIMETABLE_PR3_WARM_START_ENABLED = True
    av_on.TIMETABLE_PR3_DECISION_TRACE_ENABLED = True
    sc, _board, key, (a,) = _greedy_scenario("t-g7", pin=True)
    rule(a, KIND_UNAVAILABLE, end="13:00")

    _greedy(
        sc,
        "compact",
        baseline_placements={
            f"{key}|S1": {"day": "SUN", "start_time": "09:00", "end_time": "10:15"}
        },
    )

    meetings = _placed_meetings(sc, key)
    assert (("SUN", "09:00", "10:15") in meetings) is not flag, meetings
    assert bool(_blocked(meetings, a, _rules_of(a))) is not flag, meetings


def _online_here(*, elsewhere: bool, ds_online: bool = False):
    """GS101 is online on the AI board and carried by the DS board of the same term
    too (``ds_online``: online there as well, else a campus course there);
    ``elsewhere``: its S1 already sits on the DS board, at a time its teacher may
    teach. Its one teacher Dr A may not teach mornings."""
    sc, ai_board, key, (a,) = _greedy_scenario(
        f"t-g8 {elsewhere} {ds_online}", code="GS101", online=True, programs=("AI", "DS")
    )
    ds_board = DeliveryBoard.objects.create(
        scenario=sc, label="Term 2 DS", nominal_term=2, program="DS"
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="GS101",
        course_name="INTRO",
        programme_term=2,
        credit_hours=3,
        is_online=ds_online,
    )
    if elsewhere:
        ts = ots.section(sc, key, "S1")
        ots.place(ds_board, ts, [("MON", "13:00", "14:15"), ("WED", "13:00", "14:15")])
    rule(a, KIND_UNAVAILABLE, end="13:00")
    return sc, ai_board, key, a


#: The one online exemption (fix plan D-A): a section is exempt iff it is online on
#: EVERY board it sits on AND on every board it would be placed on (the boards of
#: its course's term). Only the first case is exempt.
ONLINE_CASES = pytest.mark.parametrize(
    ("elsewhere", "ds_online"),
    [(False, True), (False, False), (True, False)],
    ids=["online-everywhere", "campus-on-its-other-board", "campus-elsewhere"],
)


@pytest.mark.django_db
@ONLINE_CASES
def test_an_online_section_is_not_bound_by_its_teachers_rules_in_the_greedy(
    av_on, elsewhere, ds_online
) -> None:
    """T-G8 (H7, D-A), judged on the WHOLE scenario: the "morning" strategy puts a
    section online on every board in the morning (rules do not bind it). A section
    whose course is a campus course on the DS board is bound on BOTH boards, also
    while the AI board is placed before the DS board places its campus copy
    (construction:CON-1: it used to land in a forbidden morning on the AI board)."""
    from core.models import SectionPlacement

    sc, ai_board, key, a = _online_here(elsewhere=elsewhere, ds_online=ds_online)
    assert sta.load_assignment(sc.id).teacher[f"{key}_S1"] == a.pk

    _greedy(sc, "morning")

    assert len(_placed_meetings(sc, key, board=ai_board)) == 2
    boards = set(
        SectionPlacement.objects.filter(
            board__scenario=sc, term_section__course_key=key
        ).values_list("board__program", flat=True)
    )
    assert boards == {"AI", "DS"}
    meetings = _placed_meetings(sc, key)  # every board
    assert bool(_blocked(meetings, a, _rules_of(a))) is ds_online, meetings
    exempt = helper.online_exempt_sections(sc.id)
    assert ((key, "S1") in exempt) is ds_online


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_an_english_section_of_a_no_mornings_teacher_gets_the_afternoon_bundle(av_on, flag) -> None:
    """T-G9: a meeting-policy (ENG) course moves only as a whole bundle; a "no
    mornings" teacher's section takes the PM bundle (control: flag off, AM)."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _board, key, (a,) = _greedy_scenario("t-g9", code="ENG101")
    rule(a, KIND_UNAVAILABLE, end="13:00")

    _greedy(sc, "morning")

    meetings = _placed_meetings(sc, key)
    assert meetings, "the ENG section was not placed"
    starts = {start for _day, start, _end in meetings}
    assert (min(starts) >= "13:00") is flag, meetings


def _clash_taken(name: str, *, pinned: bool):
    """The teacher may teach only SUN and TUE 13:00-14:15, so the course's one
    legal week is SUN 13:00 + TUE 13:00; his pinned CS200 S1 already holds SUN
    13:00, so a clash takes that week."""
    from core.services.course_identity import planner_course_key

    sc, board, key, (a,) = _greedy_scenario(name, pin=pinned)
    ProgrammeRequirement.objects.create(
        program="AI", course_code="CS200", course_name="OTHER", programme_term=2, credit_hours=3
    )
    other = planner_course_key("CS200", "OTHER")
    ots.budget(sc, other, 1, code="CS200")
    ots.link("AI", "CS200", a)
    ts = ots.section(sc, other, "S1")
    ots.place(board, ts, [("SUN", "13:00", "14:15"), ("TUE", "14:30", "15:45")])
    _pin(sc, other, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, ("SUN", "TUE"), "13:00", "14:15")
    return sc, key, a


@pytest.mark.django_db
def test_a_fixed_teacher_whose_legal_weeks_are_all_taken_is_still_placed(av_on) -> None:
    """T-G10 (H1): the legal week clashes, so the pinned section falls back to its
    best forbidden option (the feed keeps them after the legal ones) and is
    reported; it is never left unplaced because of a rule."""
    sc, key, a = _clash_taken("t-g10", pinned=True)

    result = _greedy(sc)

    meetings = _placed_meetings(sc, key)
    assert len(meetings) == 2 and _blocked(meetings, a, _rules_of(a)), meetings
    assert _board_result(result)["instructor_availability"]["ranked_last"] == [
        {"section": f"{key}|S1", "instructor_ids": [a.pk], "reason": "rule_cannot_be_met"}
    ]


@pytest.mark.django_db
def test_an_engine_teacher_whose_legal_weeks_are_all_taken_is_reported_binding(av_on) -> None:
    """T-G11 (H1): as an ENGINE pick the same section is left unplaced; the options
    the feed left out pass clash, cap and same-course, so the rule was binding:
    listed, and counted under ``INSTRUCTOR_UNAVAILABLE`` beside the clash."""
    from core.services.timetable_decision_trace import INSTRUCTOR_CLASH, INSTRUCTOR_UNAVAILABLE

    sc, key, a = _clash_taken("t-g11", pinned=False)

    with sta.pinned_assignment(sc.id, _engine_map(sc, key, a)):
        result = _greedy(sc)

    assert _placed_meetings(sc, key) == []
    board = _board_result(result)
    (failure,) = board["room_failures"]
    assert failure["context"]["rejected_by"][INSTRUCTOR_CLASH] >= 1
    assert failure["context"]["rejected_by"][INSTRUCTOR_UNAVAILABLE] >= 1
    assert board["instructor_availability"] == {
        "ranked_last": [],
        "unplaced": [{"section": f"{key}|S1", "instructor_ids": [a.pk], "binding": True}],
    }


@pytest.mark.django_db
def test_a_reinstated_section_of_a_pinned_teacher_without_a_legal_week_is_listed(av_on) -> None:
    """T-W5 (C.21): the required-section reinstatement carries the placer's
    report, so the section placed against its pinned teacher's rule is listed."""
    from core.services.timetable_required_sections import reinstate_missing_required_sections

    sc, _board, key, (a,) = _greedy_scenario("t-w5", pin=True)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    report = reinstate_missing_required_sections(sc.id)

    assert report["after_missing"] == []
    assert report["instructor_availability"] == {
        "ranked_last": [
            {"section": f"{key}|S1", "instructor_ids": [a.pk], "reason": "rule_cannot_be_met"}
        ],
        "unplaced": [],
    }


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_the_greedy_honours_the_rules_with_the_clash_flag_off(av_on, flag) -> None:
    """T-G4 without ``TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED``: the greedy reads the
    teacher map itself, so the "morning" strategy still cannot put a "no
    mornings" teacher's section in a morning (control: flag off, it does)."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    av_on.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = False
    sc, _board, key, (a,) = _greedy_scenario("t-g4 clash off")
    rule(a, KIND_UNAVAILABLE, end="13:00")

    result = _greedy(sc, "morning")

    meetings = _placed_meetings(sc, key)
    assert len(meetings) == 2
    assert bool(_blocked(meetings, a, _rules_of(a))) is not flag, meetings
    assert "teacher_guard" not in _board_result(result)  # no clash: no guard either


@pytest.mark.django_db
@pytest.mark.parametrize("rules", [False, True], ids=["zero-rules", "rule-control"])
def test_with_the_clash_flag_off_the_greedy_reads_the_map_only_when_a_rule_exists(
    av_on, monkeypatch, rules
) -> None:
    """D-L (construction:CON-5, identity:F5): with the clash flag off the greedy
    asks ``availability_for`` first and reads the teacher map (an engine read,
    ``"raise"``) only when a rule binds: with no rule row it makes no engine read
    at all, so it cannot raise where the flag-off greedy did not read the map.
    Either way the rules are read once."""
    import sys

    av_on.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = False
    sc, _board, _key, (a,) = _greedy_scenario(f"clash off {rules}")
    if rules:
        rule(a, KIND_UNAVAILABLE, end="13:00")
    real, engine_reads = sta.load_assignment, []

    def spy(*args, **kwargs):
        caller = sys._getframe(1).f_code.co_name
        if kwargs.get("incomplete", "raise") == "raise" and caller == "auto_place_board":
            engine_reads.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(sta, "load_assignment", spy)
    with CaptureQueriesContext(connection) as ctx:
        _greedy(sc, "morning")

    assert len(engine_reads) == (1 if rules else 0)
    assert len(rule_queries(ctx.captured_queries)) == 1


@pytest.mark.django_db
def test_with_the_clash_flag_off_a_zero_rule_greedy_costs_one_query(av_on) -> None:
    """D-L (construction:CON-5): clash flag off, one teacher on, no rule row: the
    greedy costs exactly the flag-off queries plus the one rule query."""
    av_on.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = False
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    _greedy(_greedy_scenario("warm-up", code="CS100")[0], "compact")  # process-wide caches

    counts = {}
    for flag, code in ((False, "CS101"), (True, "CS102")):
        av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
        sc = _greedy_scenario(f"cost {flag}", code=code)[0]
        with CaptureQueriesContext(connection) as ctx:
            _greedy(sc, "compact")
        counts[flag] = len(ctx.captured_queries)

    assert counts[True] == counts[False] + 1, counts


@pytest.mark.django_db
@pytest.mark.parametrize("online", [True, False], ids=["online", "campus"])
def test_the_greedy_hands_the_placer_guard_its_rules_and_the_online_exemption(
    av_on, monkeypatch, online
) -> None:
    """The guard ranks a to-be-decided section's options by its open teachers'
    rules (``PlacerGuard(..., availability)``) unless the section is online on
    every board (``availability_exempt``)."""
    real = sta.PlacerGuard
    seen: dict = {"checks": []}

    class Spy(real):
        def __init__(self, assignment, availability=EMPTY_AVAILABILITY) -> None:
            super().__init__(assignment, availability)
            seen["availability"] = availability

        def check(self, key, teachers, schedule, *, availability_exempt=False):
            seen["checks"].append((key, availability_exempt))
            return super().check(key, teachers, schedule, availability_exempt=availability_exempt)

    monkeypatch.setattr(sta, "PlacerGuard", Spy)
    sc, _board, key, (a,) = _greedy_scenario(f"guard {online}", online=online)
    rule(a, KIND_UNAVAILABLE, end="13:00")

    _greedy(sc)

    assert seen["availability"].instructor_ids == {a.pk}
    assert seen["checks"] == [(f"{key}|S1", online)]


@pytest.mark.django_db
def test_a_place_missing_run_reads_the_rules_once(av_on) -> None:
    """``run_placer`` is one rule scope: its map reads, every board's greedy and
    the final ensure share one rule query."""
    from core.services.timetable_autoplace import auto_place_scenario

    sc, _board, key, (a,) = _greedy_scenario("one scope")
    rule(a, KIND_UNAVAILABLE, end="13:00")

    with CaptureQueriesContext(connection) as ctx:
        sta.run_placer(sc.id, lambda: auto_place_scenario(sc.id), rebuild=False)

    assert len(rule_queries(ctx.captured_queries)) == 1
    assert not _blocked(_placed_meetings(sc, key), a, _rules_of(a))


# ── C.4: both board CP-SAT builders ────────────────────────────────────

CPSAT_BUILDERS = ("solve_board", "solve_board_with_hints")


class _Captured(Exception):
    pass


def _solve(builder: str, board) -> dict:
    from core.services import timetable_solver

    if builder == "solve_board":
        return timetable_solver.solve_board(board.id, time_limit_seconds=10)
    return timetable_solver.solve_board_with_hints(board.id, [], time_limit_seconds=10)


def _domain_sizes(monkeypatch, builder: str, board) -> list[int]:
    """Options per meeting of the board's first section in the model ``builder``
    builds (the solve is stopped before it starts)."""
    from ortools.sat.python import cp_model

    seen: list = []

    def spy(self, model, *args, **kwargs):
        seen.append(model)
        raise _Captured

    with monkeypatch.context() as patch:
        patch.setattr(cp_model.CpSolver, "solve", spy)
        with pytest.raises(_Captured):
            _solve(builder, board)
    sizes: dict[int, int] = {}
    for variable in seen[-1].proto.variables:
        match = re.fullmatch(r"a_0_(\d+)_(\d+)", variable.name)
        if match:
            sizes[int(match.group(1))] = sizes.get(int(match.group(1)), 0) + 1
    return [sizes[m] for m in sorted(sizes)]


@pytest.mark.django_db
@pytest.mark.parametrize("builder", CPSAT_BUILDERS)
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_cpsat_option_domains_leave_out_a_ruled_teachers_forbidden_cells(
    av_on, monkeypatch, builder, flag
) -> None:
    """T-C1: "no mornings" leaves each meeting the afternoon lecture periods only
    (13:00, 14:30, 14:45, 16:00 on five days = 20 of 40); flag off, all 40."""
    from core.services.timetable_autoplace import DEFAULT_SLOTS, placeable_slots

    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, board, _key, (a,) = _greedy_scenario(f"t-c1 {builder}")
    rule(a, KIND_UNAVAILABLE, end="13:00")
    every = 5 * len(placeable_slots(DEFAULT_SLOTS))
    afternoon = 5 * sum(slot["start"] >= "13:00" for slot in placeable_slots(DEFAULT_SLOTS))

    assert _domain_sizes(monkeypatch, builder, board) == [afternoon if flag else every] * 2
    assert afternoon < every


@pytest.mark.django_db
@pytest.mark.parametrize("builder", CPSAT_BUILDERS)
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_a_cpsat_board_places_a_ruled_section_legally(av_on, builder, flag) -> None:
    """T-C2: the teacher may teach only SUN and TUE 16:00-17:15, one legal week;
    the solver places the section there (control: flag off, it does not)."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, board, _key, (a,) = _greedy_scenario(f"t-c2 {builder}")
    rule(a, KIND_AVAILABLE_ONLY, ("SUN", "TUE"), "16:00", "17:15")

    result = _solve(builder, board)

    assert result["status"] in ("optimal", "feasible") and result["placed"] == 1
    meetings = [(m["day"], m["start"], m["end"]) for m in result["placements"][0]["meetings"]]
    assert bool(_blocked(meetings, a, _rules_of(a))) is not flag, meetings


@pytest.mark.django_db
@pytest.mark.parametrize("builder", CPSAT_BUILDERS)
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_a_cpsat_english_section_keeps_only_the_bundle_its_teacher_may_teach(
    av_on, monkeypatch, builder, flag
) -> None:
    """T-C3: a "no mornings" teacher's ENG section keeps the PM bundle only (one
    option per meeting instead of two), and is placed in it."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, board, _key, (a,) = _greedy_scenario(f"t-c3 {builder}", code="ENG101")
    rule(a, KIND_UNAVAILABLE, end="13:00")

    sizes = _domain_sizes(monkeypatch, builder, board)
    result = _solve(builder, board)

    assert set(sizes) == ({1} if flag else {2})
    starts = {m["start"] for m in result["placements"][0]["meetings"]}
    if flag:
        assert min(starts) >= "13:00", starts


@pytest.mark.django_db
@pytest.mark.parametrize("builder", CPSAT_BUILDERS)
def test_a_cpsat_board_still_places_a_fixed_teacher_without_a_legal_week(av_on, builder) -> None:
    """T-C5: a pinned "only Thursday" teacher has no 2 x 75 week; his section's
    domains are not pruned (pruned, both meetings would need Thursday and the
    board would come back infeasible), so the board solves and places it."""
    sc, board, _key, (a,) = _greedy_scenario(f"t-c5 {builder}", pin=True)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    result = _solve(builder, board)

    assert result["status"] in ("optimal", "feasible"), result["status"]
    assert result["placed"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize("builder", CPSAT_BUILDERS)
def test_a_cpsat_engine_section_without_a_legal_week_makes_the_board_infeasible(
    av_on, builder
) -> None:
    """T-C5's other side: the same teacher charged as an ENGINE pick is pruned to
    no week, so the builder answers infeasible and its caller keeps the greedy
    board: the rule is never broken by CP-SAT."""
    sc, board, key, (a,) = _greedy_scenario(f"t-c5 engine {builder}")
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    with sta.pinned_assignment(sc.id, _engine_map(sc, key, a)):
        result = _solve(builder, board)

    assert result["status"] == "infeasible" and result["placed"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize("builder", CPSAT_BUILDERS)
@ONLINE_CASES
def test_cpsat_does_not_prune_an_online_section_unless_it_is_campus_elsewhere(
    av_on, monkeypatch, builder, elsewhere, ds_online
) -> None:
    """H7 (D-A) in the CP-SAT builders: a section online on every board of its
    term keeps all 40 cells a meeting; once its course is a campus course on
    another board of its term (placed there or not yet), its rules bind it and
    the mornings go."""
    from core.services.timetable_autoplace import DEFAULT_SLOTS, placeable_slots

    sc, ai_board, _key, _a = _online_here(elsewhere=elsewhere, ds_online=ds_online)
    every = 5 * len(placeable_slots(DEFAULT_SLOTS))
    afternoon = 5 * sum(slot["start"] >= "13:00" for slot in placeable_slots(DEFAULT_SLOTS))

    sizes = _domain_sizes(monkeypatch, builder, ai_board)

    assert sizes == [every if ds_online else afternoon] * 2


# ── C.5 / C.6: annealing, the load balancer and their persist guard ────

MOVERS = ("annealing", "balancer")
AFTERNOON = [("SUN", "13:00", "14:15"), ("TUE", "13:00", "14:15")]


def _placed_ruled_board(name: str, *, code: str = "CS101", meetings=AFTERNOON):
    """CS101 S1 placed (default SUN and TUE 13:00), its stored engine teacher Dr A
    may not teach mornings."""
    sc, board, key, (a,) = _greedy_scenario(name, code=code)
    ts = ots.section(sc, key, "S1")
    ots.place(board, ts, meetings)
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_UNAVAILABLE, end="13:00")
    return sc, board, key, a


def _reward_sunday_morning(schedule, sections, *args, **kwargs) -> float:
    """The stage's own cost prefers SUN 09:00 (same day: the teacher's days and
    idle time do not change), so only the rules can keep the meeting away."""
    for meetings in schedule.values():
        if any((m["day"], m["start"]) == ("SUN", "09:00") for m in meetings):
            return -1000.0
    return 0.0


def _run_mover(monkeypatch, mover: str, board) -> dict:
    from core.services import timetable_load_balanced as balancer
    from core.services import timetable_local_search as sa

    if mover == "annealing":
        monkeypatch.setattr(sa, "_compute_cost", _reward_sunday_morning)
        return sa.optimize_board(board.id, max_seconds=1.5)
    monkeypatch.setattr(balancer, "_compute_balance_score", _reward_sunday_morning)
    return balancer.rebalance_board(board.id, max_seconds=1.5)


@pytest.mark.django_db
@pytest.mark.parametrize("mover", MOVERS)
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_a_relocating_mover_never_moves_a_meeting_into_a_forbidden_cell(
    av_on, monkeypatch, mover, flag
) -> None:
    """T-S1 (annealing) / T-S2 (load balancer): the cost rewards SUN 09:00, the
    teacher may not teach mornings; the meeting stays in the afternoon
    (control: flag off, it moves to SUN 09:00)."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    _sc, board, _key, a = _placed_ruled_board(f"t-s {mover}")

    result = _run_mover(monkeypatch, mover, board)

    assert result["status"] == "optimized"
    final = [(m["day"], m["start"], m["end"]) for ms in result["schedule"].values() for m in ms]
    assert bool(_blocked(final, a, _rules_of(a))) is not flag, final


def _relocate_domains(monkeypatch, board) -> dict:
    from tests._online_windows_fixture import capture_relocate_domains

    return capture_relocate_domains(monkeypatch, board)


def _cell_blocked(person, rules, cell) -> bool:
    return helper.option_blocked(
        rules, (person.pk,), [{"day": cell[0], "start": cell[1], "end": cell[2]}]
    )


@pytest.mark.django_db
def test_relocate_domains_lose_exactly_a_ruled_teachers_forbidden_cells(av_on, monkeypatch) -> None:
    """T-S3: for both movers the ruled section's domains are the flag-off domains
    minus exactly its teacher's forbidden cells; an unruled section's are equal."""
    from core.services.course_identity import planner_course_key

    sc, board, key, a = _placed_ruled_board("t-s3")
    ProgrammeRequirement.objects.create(
        program="AI", course_code="CS102", course_name="OTHER", programme_term=2, credit_hours=3
    )
    other = planner_course_key("CS102", "OTHER")
    ots.budget(sc, other, 1, code="CS102")
    ots.link("AI", "CS102", _person("Dr B"))
    ots.place(
        board, ots.section(sc, other, "S1"), [("MON", "09:00", "10:15"), ("WED", "09:00", "10:15")]
    )
    rules = _rules_of(a)

    on = _relocate_domains(monkeypatch, board)
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    off = _relocate_domains(monkeypatch, board)

    for mover in ("sa", "lb"):
        ruled, plain = on[mover][f"{key}|S1"], on[mover][f"{other}|S1"]
        expected = [
            [cell for cell in meeting if not _cell_blocked(a, rules, cell)]
            for meeting in off[mover][f"{key}|S1"]["domain"]
        ]
        assert ruled["domain"] == expected
        assert all(
            len(m) < len(o)
            for m, o in zip(expected, off[mover][f"{key}|S1"]["domain"], strict=True)
        )
        assert plain == off[mover][f"{other}|S1"]


@pytest.mark.django_db
@pytest.mark.parametrize("mover", MOVERS)
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_an_english_section_moves_only_into_a_bundle_its_teacher_may_teach(
    av_on, monkeypatch, mover, flag
) -> None:
    """T-S4: an ENG section of a "no mornings" teacher, placed in the AM bundle,
    keeps only the PM bundle in the mover's whole-bundle domain (control: both)."""
    from core.services import timetable_load_balanced as balancer
    from core.services import timetable_local_search as sa
    from core.services.timetable_autoplace import generate_course_meeting_options

    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    am, pm = sorted(
        generate_course_meeting_options(
            "ENG101", 0, [], [], [], course_key="ENG101", meeting_pattern=""
        ),
        key=lambda bundle: bundle[0]["start"],
    )
    _sc, board, _key, _a = _placed_ruled_board(
        f"t-s4 {mover}", code="ENG101", meetings=[(m["day"], m["start"], m["end"]) for m in am]
    )
    seen: dict = {}

    def spy(schedule, sections, valid_options, rng, locked_idx=None):
        seen["bundles"] = [
            [(m["day"], m["start"]) for m in bundle] for bundle in sections[0].get("bundles") or []
        ]
        raise _Captured

    module, run = (
        (sa, sa.optimize_board) if mover == "annealing" else (balancer, balancer.rebalance_board)
    )
    monkeypatch.setattr(module, "_generate_relocate_move", spy)
    with pytest.raises(_Captured):
        run(board.id, max_seconds=1)

    expected = [pm] if flag else [am, pm]
    assert sorted(seen["bundles"]) == sorted(
        [(m["day"], m["start"]) for m in bundle] for bundle in expected
    )


@pytest.mark.django_db
@ONLINE_CASES
def test_relocate_domains_spare_a_section_online_on_every_board(
    av_on, monkeypatch, elsewhere, ds_online
) -> None:
    """H7 (D-A) in the movers: a section online on every board of its term keeps
    its whole domain; the same section of a course that is a campus course on
    another board of its term (placed there or not) loses the mornings."""
    from core.models import TermSection

    sc, ai_board, key, a = _online_here(elsewhere=elsewhere, ds_online=ds_online)
    ts = TermSection.objects.filter(scenario=sc, course_key=key, section="S1").first()
    ots.place(ai_board, ts or ots.section(sc, key, "S1"), AFTERNOON)
    rules = _rules_of(a)

    domains = _relocate_domains(monkeypatch, ai_board)

    for mover in ("sa", "lb"):
        cells = [cell for meeting in domains[mover][f"{key}|S1"]["domain"] for cell in meeting]
        assert cells
        assert any(_cell_blocked(a, rules, cell) for cell in cells) is ds_online


@pytest.mark.django_db
@pytest.mark.parametrize("guarded", [True, False], ids=["guard", "guard-off-control"])
def test_the_persist_guard_rolls_back_a_pass_that_adds_a_violation(
    av_on, monkeypatch, guarded
) -> None:
    """T-C4: with the annealing domain prune taken out and the cost rewarding SUN
    09:00, the pass moves the meeting there; the persist guard counts one more
    forbidden meeting and rolls the pass back (control: the guard without the
    rules lets the same pass persist)."""
    from core.models import SectionPlacement
    from core.services import timetable_local_search as sa

    sc, board, key, a = _placed_ruled_board(f"t-c4 {guarded}")
    monkeypatch.setattr(sa, "prune_relocate_domains", lambda *args, **kwargs: None)
    monkeypatch.setattr(sa, "_compute_cost", _reward_sunday_morning)
    if not guarded:
        monkeypatch.setattr(sa, "availability_for", lambda *args, **kwargs: EMPTY_AVAILABILITY)

    result = sa.optimize_and_persist_board(board.id, max_seconds=1.5)

    persisted = sorted(
        SectionPlacement.objects.filter(board=board).values_list("day", "start_time", "end_time")
    )
    if guarded:
        assert result.get("persist_rolled_back") is True
        assert result["rollback_reasons"] == ["instructor availability violations increased"]
        assert persisted == sorted(AFTERNOON)
    else:
        assert not result.get("persist_rolled_back"), result.get("rollback_reasons")
        assert ("SUN", "09:00", "10:15") in persisted


@pytest.mark.django_db
@pytest.mark.parametrize("ds_online", [True, False], ids=["online-everywhere", "campus-control"])
def test_the_persist_guard_does_not_count_a_section_online_on_every_board(
    av_on, monkeypatch, ds_online
) -> None:
    """The guard's count skips a section online on every board of its term (H7,
    D-A): the pass that moves it to SUN 09:00 persists. Control: its course is a
    campus course on the DS board, so the same pass adds a violation and is
    rolled back (the annealing prune taken out, so the pass makes the move)."""
    from core.models import SectionPlacement, TermSection
    from core.services import timetable_local_search as sa

    sc, ai_board, key, a = _online_here(elsewhere=False, ds_online=ds_online)
    ots.place(ai_board, ots.section(sc, key, "S1"), AFTERNOON)
    _engine_row(sc, key, "S1", a)  # stays charged after the move (the kept rule)
    assert TermSection.objects.filter(scenario=sc).count() == 1
    monkeypatch.setattr(sa, "prune_relocate_domains", lambda *args, **kwargs: None)
    monkeypatch.setattr(sa, "_compute_cost", _reward_sunday_morning)

    result = sa.optimize_and_persist_board(ai_board.id, max_seconds=1.5)

    persisted = SectionPlacement.objects.filter(board=ai_board).values_list(
        "day", "start_time", "end_time"
    )
    if ds_online:
        assert not result.get("persist_rolled_back"), result.get("rollback_reasons")
        assert ("SUN", "09:00", "10:15") in set(persisted)
    else:
        assert result.get("persist_rolled_back") is True
        assert result["rollback_reasons"] == ["instructor availability violations increased"]
        assert sorted(persisted) == sorted(AFTERNOON)


# ── C.7-C.10: the improvement stages (catalogue, LS-v2, chain-2, polisher) ──

from core.services import timetable_local_search_chains as chains  # noqa: E402
from core.services import timetable_local_search_v2 as local_search  # noqa: E402
from core.services.timetable_assignment_models import (  # noqa: E402
    CanonicalPattern,
    RiskTier,
    StudentProfile,
    TimetableMove,
)
from core.services.timetable_candidate_eval import (  # noqa: E402
    evaluate_generated_timetable_candidate,
)

#: Section A is taught by teacher 7 (the run's map).
A_MAP = {"A": frozenset({7})}
#: Rules on (teacher 7) or the empty-availability control.
RULES_OR_EMPTY = pytest.mark.parametrize(
    "rules", [True, False], ids=["rules", "empty-availability-control"]
)


def _pat(name: str, cells, family: str = "fam") -> CanonicalPattern:
    """A canonical pattern of ``(day, (start, end))`` cells."""
    meetings = [SectionMeeting(day, start, end) for day, (start, end) in cells]
    return CanonicalPattern(
        pattern_id=name,
        signature=name,
        meetings=meetings,
        pattern_family=family,
        duration_permutation="_".join(str(m.end_min - m.start_min) for m in meetings),
        is_lab_mixed=False,
        meeting_count=len(meetings),
        days_used=frozenset(m.day for m in meetings),
        slot_fingerprint=name,
    )


def _sec(sid: str, pattern: CanonicalPattern) -> SectionState:
    return SectionState(
        section_id=sid,
        course_code=sid,
        meetings=list(pattern.meetings),
        max_capacity=30,
        reserve_capacity=0,
        pattern_id=pattern.pattern_id,
        pattern_family=pattern.pattern_family,
    )


A_PM = _pat("a_pm", [(SUN, PM), (TUE, PM)])
A_AM = _pat("a_am", [(SUN, AM), (TUE, AM)])
A_AM_PM = _pat("a_am_pm", [(SUN, AM), (TUE, PM)])  # one forbidden meeting
A_AM_PM_WED = _pat("a_am_pm_wed", [(SUN, AM), (WED, PM)])  # one forbidden meeting
B_SUN_PM = _pat("b_sun_pm", [(SUN, PM)], family="b")
B_TUE_PM = _pat("b_tue_pm", [(TUE, PM)], family="b")


def _search_board(start: CanonicalPattern, blocker: CanonicalPattern, catalog_a, extra=()):
    """A (teacher 7) clashes with the locked B for the one student taking both."""
    sections = {"A": _sec("A", start), "B": _sec("B", blocker)}
    catalog = {start.pattern_family: list(catalog_a), "b": [blocker]}
    for sid, pattern, alternatives in extra:
        sections[sid] = _sec(sid, pattern)
        catalog[pattern.pattern_family] = list(alternatives)
    profiles = {"s": StudentProfile("s", "AI", sorted(sections), RiskTier.C, 0)}
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), profiles, {}, A_MAP
    )
    return sections, catalog, profiles, baseline


def _local_search(availability, sections, catalog, profiles, baseline):
    return local_search.diagnostic_driven_local_search(
        baseline,
        sections,
        catalog,
        profiles,
        {},
        locked_section_ids={"B"},
        max_iterations=1,
        section_instructor_ids=A_MAP,
        instructor_availability=availability,
    )


@RULES_OR_EMPTY
def test_local_search_v2_never_moves_a_section_into_a_forbidden_pattern(rules) -> None:
    """T-V2: the only clash-free pattern is in the mornings teacher 7 may not teach
    (control: an empty availability takes it)."""
    sections, catalog, profiles, baseline = _search_board(A_PM, B_SUN_PM, [A_PM, A_AM])

    result = _local_search(
        NO_MORNINGS if rules else EMPTY_AVAILABILITY, sections, catalog, profiles, baseline
    )

    if rules:
        assert result is baseline
        assert sections["A"].pattern_id == "a_pm"
    else:
        assert result is not baseline
        assert sections["A"].pattern_id == "a_am"


def test_local_search_v2_lets_a_section_keep_a_violation_it_already_had() -> None:
    """T-V3 (delta): A already has one forbidden meeting and may move to another
    pattern with one forbidden meeting (the repair, not every move, removes it)."""
    sections, catalog, profiles, baseline = _search_board(A_AM_PM, B_TUE_PM, [A_AM_PM, A_AM_PM_WED])

    result = _local_search(NO_MORNINGS, sections, catalog, profiles, baseline)

    assert result is not baseline
    assert sections["A"].pattern_id == "a_am_pm_wed"


def _catalog(*predicates) -> list[CanonicalPattern]:
    from core.services.timetable_pattern_catalog import build_canonical_pattern_catalog

    catalog = build_canonical_pattern_catalog(
        [{"durations": [75, 75], "course_code": "A", "course_key": "A", "credit_hours": 3}],
        cell_predicates=predicates,
    )
    return catalog["ONCAMPUS_LEC_75_75"]


def _legal(pattern, availability=SUN_PM_AND_TUE_LATE_MORNING) -> bool:
    cells = [(m.day, m.start_min, m.end_min) for m in pattern.meetings]
    return not helper.meetings_blocked(availability, (7,), cells)


#: SUN 13:00 + TUE 10:30: a mixed-period week teacher 7 may teach.
MIXED_CELLS = [(SUN, 13 * 60, 14 * 60 + 15), (TUE, 10 * 60 + 30, 11 * 60 + 45)]


def test_the_pattern_catalogue_gains_the_mixed_weeks_a_ruled_teacher_may_teach() -> None:
    """T-V1 (catalogue): the same-period patterns first, unchanged; then the
    mixed-period weeks teacher 7 may teach (control: no predicate, none)."""
    plain = _catalog()
    ruled = _catalog(helper.cell_predicate(SUN_PM_AND_TUE_LATE_MORNING, (7,)))

    assert ruled[: len(plain)] == plain
    assert not any(_legal(p) for p in plain)
    extra = ruled[len(plain) :]
    assert extra and all(_legal(p) for p in extra)
    assert MIXED_CELLS in [
        sorted((m.day, m.start_min, m.end_min) for m in p.meetings) for p in extra
    ]


@pytest.mark.parametrize("mixed", [True, False], ids=["mixed-weeks", "same-period-control"])
def test_local_search_v2_moves_a_ruled_section_onto_a_mixed_period_week(mixed) -> None:
    """T-V1 (search): A sits in a legal mixed week clashing with B; only the
    catalogue's mixed weeks let it leave without breaking teacher 7's rules
    (control: the same-period catalogue offers no legal pattern, so it stays)."""
    rules = SUN_PM_AND_TUE_LATE_MORNING
    patterns = _catalog(helper.cell_predicate(rules, (7,))) if mixed else _catalog()
    now = _pat("a_now", [(SUN, (870, 945)), (TUE, (650, 725))], family="ONCAMPUS_LEC_75_75")
    blocker = _pat("b_now", [(SUN, (870, 945))], family="b")
    assert _legal(now)
    sections, catalog, profiles, baseline = _search_board(now, blocker, [now, *patterns])

    result = _local_search(rules, sections, catalog, profiles, baseline)

    if mixed:
        assert result is not baseline
        assert sections["A"].pattern_id != "a_now" and _legal(sections["A"])
    else:
        assert result is baseline
        assert sections["A"].pattern_id == "a_now"


C_NOW = _pat("c_now", [(MON, (900, 975))], family="c")
C_ALT = _pat("c_alt", [(MON, (960, 1035))], family="c")


def _chain_search(monkeypatch, availability, board, chain_list):
    """Chain search on exactly ``chain_list`` (the loop under test is the real one)."""
    sections, catalog, profiles, baseline = board
    monkeypatch.setattr(chains, "generate_chain_2_moves", lambda *a, **k: list(chain_list))
    return chains.chain_local_search(
        baseline,
        sections,
        catalog,
        profiles,
        {},
        locked_section_ids={"B"},
        max_iterations=1,
        section_instructor_ids=A_MAP,
        instructor_availability=availability,
    )


@RULES_OR_EMPTY
def test_chain_search_rejects_a_chain_that_moves_a_section_into_a_forbidden_pattern(
    monkeypatch, rules
) -> None:
    """T-V4: (A -> mornings, C -> elsewhere) resolves the clash but breaks teacher
    7's rules; rolled back whole (control: an empty availability takes it)."""
    board = _search_board(A_PM, B_SUN_PM, [A_PM, A_AM], extra=[("C", C_NOW, [C_NOW, C_ALT])])
    chain = chains.ChainMove(
        TimetableMove("repattern", "A", "a_pm", "a_am"),
        TimetableMove("repattern", "C", "c_now", "c_alt"),
    )
    availability = NO_MORNINGS if rules else EMPTY_AVAILABILITY

    result = _chain_search(monkeypatch, availability, board, [chain])

    sections, _catalog_, _profiles, baseline = board
    if rules:
        assert result is baseline
        assert sections["A"].pattern_id == "a_pm" and sections["C"].pattern_id == "c_now"
    else:
        assert result is not baseline
        assert sections["A"].pattern_id == "a_am"


@RULES_OR_EMPTY
def test_a_chain_moving_one_section_twice_is_judged_from_its_first_snapshot(
    monkeypatch, rules
) -> None:
    """T-V5: legal -> one forbidden meeting -> one forbidden meeting is a new
    violation (the second leg alone adds none). Control: an empty availability."""
    board = _search_board(A_PM, B_SUN_PM, [A_PM, A_AM_PM, A_AM_PM_WED])
    chain = chains.ChainMove(
        TimetableMove("repattern", "A", "a_pm", "a_am_pm"),
        TimetableMove("repattern", "A", "a_am_pm", "a_am_pm_wed"),
    )
    availability = NO_MORNINGS if rules else EMPTY_AVAILABILITY

    result = _chain_search(monkeypatch, availability, board, [chain])

    sections, _catalog_, _profiles, baseline = board
    if rules:
        assert result is baseline
        assert sections["A"].pattern_id == "a_pm"
    else:
        assert result is not baseline
        assert sections["A"].pattern_id == "a_am_pm_wed"


# ── C.10 + H6: the CP-SAT polisher ────────────────────────────────────

SUN_AM_CELL = (SUN, 9 * 60, 10 * 60 + 15)
MON_AM_CELL = (MON, 9 * 60, 10 * 60 + 15)
SUN_PM_CELL = (SUN, 13 * 60, 14 * 60 + 15)
MON_PM_CELL = (MON, 14 * 60 + 30, 15 * 60 + 45)


def _polish_case(
    monkeypatch,
    movable: dict[str, list[tuple[int, int, int]]],
    fixed: dict[str, list[tuple[int, int, int]]],
    *,
    open_cells=(SUN_AM_CELL, SUN_PM_CELL, MON_PM_CELL),
    overlap=None,
    takes=(),
    section_ids=None,
    availability=EMPTY_AVAILABILITY,
):
    """One polisher pass. Lecture cells outside ``open_cells`` are blocked for the
    scenario (``None``: nothing blocked); ``overlap`` replaces the cross-board
    student overlap the objective reads; the one student takes ``takes``.
    Returns ``(result, {section id: polished cells})``."""
    from core.models import TimetableScenario
    from core.services import timetable_cpsat_polisher as polisher
    from core.services.timetable_autoplace import DEFAULT_SLOTS, placeable_slots

    names = ("SUN", "MON", "TUE", "WED", "THU")
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", name="polish av")
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    if open_cells is not None:
        keep = {(names[d], f"{s // 60:02d}:{s % 60:02d}") for d, s, _e in open_cells}
        scenario.blocked_slots = [
            {"day": day, "start": slot["start"]}
            for day in names
            for slot in placeable_slots(DEFAULT_SLOTS)
            if (day, slot["start"]) not in keep
        ]
        scenario.save(update_fields=["blocked_slots"])
    monkeypatch.setattr(polisher, "build_cross_board_overlap_matrix", lambda _sid: overlap or {})

    def state(sid, cells):
        return SectionState(
            sid,
            sid,
            [SectionMeeting(*cell) for cell in cells],
            30,
            0,
            pattern_family="fam",
            pattern_id=f"{sid}_now",
        )

    sections = [state(sid, cells) for sid, cells in {**movable, **fixed}.items()]
    profiles = {"s": StudentProfile("s", "AI", list(takes), RiskTier.C, 0)}
    baseline = evaluate_generated_timetable_candidate(
        "current", sections, profiles, {}, section_ids
    )
    result = polisher.polish_scenario_with_cpsat(
        scenario.id,
        sections,
        profiles,
        {},
        baseline,
        time_limit_seconds=5,
        locked_section_ids=set(fixed),
        section_instructor_ids=section_ids,
        instructor_availability=availability,
    )
    polished = {
        s.section_id: sorted((m.day, m.start_min, m.end_min) for m in s.meetings)
        for s in (result["improved_sections"] if result else [])
    }
    return result, polished


@pytest.mark.django_db
@RULES_OR_EMPTY
def test_the_polisher_moves_a_ruled_section_only_into_cells_its_teacher_may_teach(
    monkeypatch, rules
) -> None:
    """T-V6: A clashes with B; the objective prefers SUN 09:00 (no overlap) to
    MON 14:30 (a smaller overlap with C). Teacher 7 may not teach mornings, so A
    goes to MON 14:30 (control: an empty availability takes SUN 09:00)."""
    result, polished = _polish_case(
        monkeypatch,
        {"A": [SUN_PM_CELL]},
        {"B": [SUN_PM_CELL], "C": [MON_PM_CELL]},
        overlap={("A", "B"): 2, ("A", "C"): 1},
        takes=("A", "B"),
        section_ids=A_MAP,
        availability=NO_MORNINGS if rules else EMPTY_AVAILABILITY,
    )

    assert result is not None
    assert polished["A"] == [MON_PM_CELL if rules else SUN_AM_CELL]


@pytest.mark.django_db
def test_the_polisher_lets_a_meeting_stay_in_its_forbidden_current_slot(monkeypatch) -> None:
    """C.10 current-slot exemption: R already teaches at SUN 09:00 (forbidden) and
    its only legal cell overlaps C; X's move carries the pass and R stays."""
    result, polished = _polish_case(
        monkeypatch,
        {"R": [SUN_AM_CELL], "X": [SUN_PM_CELL]},
        {"B": [SUN_PM_CELL], "C": [MON_PM_CELL]},
        overlap={("C", "R"): 1},
        takes=("B", "X"),
        section_ids={"R": frozenset({7})},
        availability=NO_MORNINGS,
    )

    assert result is not None
    assert polished["R"] == [SUN_AM_CELL]
    assert polished["X"] != [SUN_PM_CELL]


@pytest.mark.django_db
@RULES_OR_EMPTY
def test_a_ruled_section_with_no_legal_cell_is_held_fixed_and_the_rest_polished(
    monkeypatch, rules
) -> None:
    """H6: R sits off the grid (SUN 08:00) and every open cell is a morning its
    teacher may not teach: R is held fixed like a locked section, and X is still
    polished (otherwise R's empty domain would abort the whole pass). Control: an
    empty availability polishes R too."""
    off_grid = (SUN, 8 * 60, 9 * 60 + 15)
    result, polished = _polish_case(
        monkeypatch,
        {"R": [off_grid], "X": [SUN_AM_CELL]},
        {"B": [SUN_AM_CELL]},
        open_cells=(SUN_AM_CELL, MON_AM_CELL),
        overlap={("B", "X"): 2},
        takes=("B", "X"),
        section_ids={"R": frozenset({7})},
        availability=NO_MORNINGS if rules else EMPTY_AVAILABILITY,
    )

    assert result is not None
    assert polished["X"] == [MON_AM_CELL]
    assert ("R" in polished) is not rules  # held fixed: never re-timed


@pytest.mark.django_db
@RULES_OR_EMPTY
def test_the_polisher_never_switches_an_english_section_into_a_forbidden_bundle(
    monkeypatch, rules
) -> None:
    """T-V7 (H5): ENG101 teaches the afternoon bundle and clashes with B on SUN;
    the morning bundle would resolve it, but teacher 7 may not teach mornings, so
    the whole morning bundle is closed (control: an empty availability switches)."""
    afternoon = [(day, 13 * 60, 16 * 60) for day in (SUN, MON, WED, THU)]
    morning = [(day, 9 * 60, 12 * 60) for day in (SUN, MON, WED, THU)]
    result, polished = _polish_case(
        monkeypatch,
        {"ENG101": afternoon},
        {"B": [SUN_PM_CELL]},
        open_cells=None,
        overlap={("B", "ENG101"): 2},
        takes=("B", "ENG101"),
        section_ids={"ENG101": frozenset({7})},
        availability=NO_MORNINGS if rules else EMPTY_AVAILABILITY,
    )

    if rules:
        assert result is None or polished["ENG101"] == afternoon
    else:
        assert result is not None
        assert polished["ENG101"] == morning


# ── C.14: the evaluator side-band and the veto net ────────────────────


def _side_band(evaluation, violations: int):
    evaluation.quality_score["instructor_availability"] = {"violations": violations}
    return evaluation


@pytest.mark.parametrize("violations", [1, 0], ids=["rise", "no-rise-control"])
def test_every_overload_veto_also_vetoes_a_rise_in_rule_violations(settings, violations) -> None:
    """T-E1: a candidate better in every other respect is refused by
    ``evaluation_is_better``, ``DayOffPolicy.core`` / ``step`` and the recovery's
    ``_student_safety_ok`` when its side-band count rises (control: no rise)."""
    from core.services.timetable_optimisation_policy import evaluation_is_better
    from core.services.timetable_secondary_recovery import _student_safety_ok
    from tests.test_instructor_day_off_policy import board, policy_at, student

    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = False
    days = {"7": 2}
    incumbent = _side_band(board(days), 0)
    candidate = _side_band(board(days, score=student(gap=-10)), violations)
    policy = policy_at(days)
    accepted = violations == 0

    assert evaluation_is_better(candidate, incumbent) is accepted
    assert policy.core(candidate, incumbent) is accepted
    assert policy.step(candidate, incumbent, base=incumbent) is accepted
    assert _student_safety_ok(candidate, incumbent) is accepted


@pytest.mark.django_db
def test_the_evaluator_side_band_exists_only_inside_a_scope_that_loaded_rules(av_on) -> None:
    """T-E2: with the rules loaded by the run's scope the evaluator counts the
    forbidden meetings of charged sections (online ones left out); outside a scope,
    or with no rule, there is no key at all."""
    sc, people = _course_scenario("t-e2", {"Dr Seven": "AI"})
    person = people["Dr Seven"]
    sections = [
        SectionState("CS101_S1", "CS101", [SectionMeeting(SUN, *AM)], 30, 0),
        SectionState("GS101_S1", "GS101", [SectionMeeting(SUN, *AM)], 30, 0, is_online=True),
    ]
    ids = {"CS101_S1": frozenset({person.pk}), "GS101_S1": frozenset({person.pk})}

    def side_band():
        result = evaluate_generated_timetable_candidate("x", sections, {}, {}, ids)
        return result.quality_score.get("instructor_availability")

    with helper.availability_scope(sc.id):
        assert not helper.availability_for(sc.id)
        assert side_band() is None  # flag on, no rule
    rule(person, KIND_UNAVAILABLE, end="13:00")
    with helper.availability_scope(sc.id):
        assert helper.availability_for(sc.id)
        assert side_band() == {"violations": 1}
    assert side_band() is None  # unbound: the evaluator never loads


# ── C.18 + H10: the optimiser wiring ──────────────────────────────────


def test_the_catalogue_predicates_follow_only_the_teachers_the_map_charges() -> None:
    """H10: one predicate per ruled teacher the run's map CHARGES. A rule on a
    teacher nobody is charged with (8) adds no pattern, so LS-v2 stays as without
    rules; teacher 9 is charged but has no rule."""
    from core.services.timetable_optimizer_v2 import _catalog_cell_predicates

    rules = _av(
        (7, KIND_UNAVAILABLE, ("SUN", "MON", "TUE", "WED", "THU"), 0, 13 * 60),
        (8, KIND_UNAVAILABLE, ("SUN",), 0, 24 * 60),
    )

    (predicate,) = _catalog_cell_predicates(rules, {"A": frozenset({7}), "B": frozenset({9})})
    assert predicate("SUN", "09:00", "10:15") and not predicate("SUN", "13:00", "14:15")
    assert _catalog_cell_predicates(rules, {"B": frozenset({9})}) == []
    assert _catalog_cell_predicates(EMPTY_AVAILABILITY, {"A": frozenset({7})}) == []
    assert _catalog_cell_predicates(rules, None) == []


@pytest.mark.django_db
def test_the_scenario_catalogue_holds_a_charged_ruled_teachers_mixed_weeks(av_on) -> None:
    """C.7 at the scenario: the catalogue built for a run with a charged ruled
    teacher keeps every same-period pattern first and adds his mixed weeks
    (control: the same call without the map, or without rules, is unchanged)."""
    from core.services.timetable_optimizer_v2 import _build_pattern_catalog_for_scenario

    sc, _board, _key, (a,) = _greedy_scenario("c7 scenario")
    plain = _build_pattern_catalog_for_scenario(sc.id)
    rules = _av(
        (a.pk, KIND_AVAILABLE_ONLY, ("SUN",), 13 * 60, 17 * 60 + 15),
        (a.pk, KIND_AVAILABLE_ONLY, ("TUE",), 10 * 60 + 30, 12 * 60 + 5),
    )
    charged = {"X_S1": frozenset({a.pk})}

    ruled = _build_pattern_catalog_for_scenario(
        sc.id, availability=rules, section_instructor_ids=charged
    )

    assert set(ruled) == set(plain)
    family = next(iter(plain))
    assert ruled[family][: len(plain[family])] == plain[family]
    assert len(ruled[family]) > len(plain[family])
    assert _build_pattern_catalog_for_scenario(sc.id, availability=rules) == plain
    assert (
        _build_pattern_catalog_for_scenario(
            sc.id, availability=EMPTY_AVAILABILITY, section_instructor_ids=charged
        )
        == plain
    )


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["current", "full"])
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_optimise_current_hands_the_runs_rules_to_the_searches_and_the_polisher(
    av_on, monkeypatch, flag, mode
) -> None:
    """C.18 (Optimise Current and Generate): with priority, the idle gap, the cap and the clash rule all off, the
    rules alone make the run build its teacher map; LS-v2, chain-2 and the polisher
    each receive that map and the run's ONE availability object, and the catalogue
    is built for them (control: flag off, no map, an empty availability)."""
    from core.services import timetable_cpsat_polisher as polisher
    from core.services import timetable_optimizer_v2 as v2

    for name in (
        "TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED",
        "TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED",
        "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED",
        "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED",
    ):
        setattr(av_on, name, False)
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _board, key, a = _placed_ruled_board(f"c18 {flag}")
    profile = StudentProfile("s", "AI", [key], RiskTier.C, 0)
    monkeypatch.setattr(v2, "build_student_profiles_for_scenario", lambda *a, **k: {"s": profile})
    seen: dict[str, tuple] = {}

    def spy(name, module, attr):
        real = getattr(module, attr)

        def inner(*args, **kwargs):
            seen[name] = (
                kwargs.get("instructor_availability", kwargs.get("availability")),
                kwargs.get("section_instructor_ids"),
            )
            return real(*args, **kwargs)

        monkeypatch.setattr(module, attr, inner)

    spy("local_search", local_search, "diagnostic_driven_local_search")
    spy("chain", chains, "chain_local_search")
    spy("polisher", polisher, "polish_scenario_with_cpsat")
    spy("catalogue", v2, "_build_pattern_catalog_for_scenario")

    with CaptureQueriesContext(connection) as captured:
        if mode == "current":
            v2.optimise_current_timetable(
                sc.id, max_search_iterations=1, max_chain_iterations=1, cpsat_time_limit=1
            )
        else:
            v2.optimise_scenario_timetable_v2(
                sc.id,
                strategies=["compact"],
                max_search_iterations=1,
                max_chain_iterations=1,
                cpsat_time_limit=1,
            )

    # Each public entry point is one scope: one rule query (none with the flag off).
    assert len(rule_queries(captured.captured_queries)) == (1 if flag else 0)
    assert set(seen) == {"local_search", "chain", "polisher", "catalogue"}
    objects = {id(availability) for availability, _ids in seen.values()}
    assert len(objects) == 1
    for availability, ids in seen.values():
        assert bool(availability) is flag
        assert (ids is not None) is flag
    if flag:
        availability, ids = seen["polisher"]
        assert availability.instructor_ids == {a.pk}
        assert ids == {f"{key}_S1": frozenset({a.pk})}


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["current", "full"])
def test_a_guarded_optimiser_run_reads_the_rules_once(av_on, monkeypatch, mode) -> None:
    """T-P3 (B.4) for the runner: ``run_v2_optimisation_guarded`` is one scope, so
    its pre-step, every phase and the pipeline issue ONE rule query (control:
    the same run without the runner's scope reads them more than once)."""
    from core.services import timetable_optimizer_v2 as v2
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    sc, _board, key, _a = _placed_ruled_board(f"t-p3 runner {mode}")
    profile = StudentProfile("s", "AI", [key], RiskTier.C, 0)
    monkeypatch.setattr(v2, "build_student_profiles_for_scenario", lambda *a, **k: {"s": profile})

    with CaptureQueriesContext(connection) as captured:
        result = run_v2_optimisation_guarded(
            sc.id,
            mode=mode,
            max_iterations=1,
            run_chain=False,
            run_cpsat=False,
            strategies=["compact"],
        )

    assert "error" not in result, result.get("error")
    assert len(rule_queries(captured.captured_queries)) == 1


# ── C.11-C.13, C.16: repairs, compaction, fixed patterns, recovery ─────
# These stages read the rules through ``availability_for`` like every other; the
# boards below are the day-run precedent's one-instructor boards, so the tests
# hand the stage its availability directly (the read and its gating are pinned
# above) and the control hands it an empty one.

from core.models import SectionPlacement  # noqa: E402
from tests.test_section_day_run import (  # noqa: E402
    _clash_scenario,
    _compaction_scenario,
    _course_days,
    _instructor_board,
    _overload_scenario,
    _safe_search_scenario,
)


def _inject(monkeypatch, availability) -> None:
    """Every repair-stage read of the rules returns ``availability``."""
    from core.services import timetable_instructor_cap_repair as cap_repair
    from core.services import timetable_instructor_compaction as compaction

    def read(_scenario_id, **_kwargs):
        return availability

    for module in (helper, cap_repair, compaction):
        monkeypatch.setattr(module, "availability_for", read)


def _dr(name: str = "Dr Run") -> int:
    return Instructor.objects.get(full_name=name).pk


def _unavailable(person: int, *days: str):
    return _av(*((person, KIND_UNAVAILABLE, (day,), 0, 24 * 60) for day in days))


REPAIR_FLAGS = {
    "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP": 3,
    "TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED": False,
    "TIMETABLE_SECTION_DAY_SPREAD": False,
}


@pytest.fixture
def repair_flags(settings):
    for name, value in REPAIR_FLAGS.items():
        setattr(settings, name, value)
    return settings


@pytest.mark.django_db(transaction=True)
@RULES_OR_EMPTY
def test_cap_repair_relocates_to_a_day_its_teacher_may_teach(
    repair_flags, monkeypatch, rules
) -> None:
    """T-R1: THU holds four sessions (cap 3); C1's THU session may go to MON or WED
    and MON comes first. Dr Run may not teach on MON, so it goes to WED (control:
    an empty availability takes MON)."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads

    scenario = _overload_scenario(f"t-r1 {rules}")
    _inject(monkeypatch, _unavailable(_dr(), "MON") if rules else EMPTY_AVAILABILITY)

    report = repair_instructor_daily_overloads(scenario.id)

    assert report["remaining_violations"] == 0 and report["unplaced"] == []
    assert _course_days(scenario, "C1") == (
        ["SUN", "TUE", "WED"] if rules else ["SUN", "MON", "TUE"]
    )


@pytest.mark.django_db(transaction=True)
@RULES_OR_EMPTY
def test_cap_repair_leaves_the_overload_rather_than_break_a_rule_or_unplace(
    repair_flags, monkeypatch, rules
) -> None:
    """T-R2 (owner decision G.2): Dr Run may teach on THU only, so every
    cap-relieving relocation of every THU session is a day he may not teach: the
    over-cap THU stays, reported, and nothing is unplaced (control: an empty
    availability relocates)."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads

    scenario = _overload_scenario(f"t-r2 {rules}")
    only_thu = _unavailable(_dr(), "SUN", "MON", "TUE", "WED")
    _inject(monkeypatch, only_thu if rules else EMPTY_AVAILABILITY)

    report = repair_instructor_daily_overloads(scenario.id)

    assert report["unplaced"] == []
    if rules:
        assert report["repaired"] == []
        assert report["remaining_violations"] == 1
        assert report["locked_blocked"][0]["reason"] == "instructor_unavailable_elsewhere"
        assert {row["section"] for row in report["availability_blocked"]} == {
            "C1 S1",
            "C2 S1",
            "C3 S1",
            "C4 S1",
        }
        assert "THU" in _course_days(scenario, "C1")
    else:
        assert report["remaining_violations"] == 0
        assert "availability_blocked" not in report


@pytest.mark.django_db(transaction=True)
@RULES_OR_EMPTY
def test_cap_repair_with_no_relocation_at_all_still_unplaces_as_before(
    repair_flags, monkeypatch, rules
) -> None:
    """T-R3: MON and WED are closed for the scenario, so no relocation exists even
    ignoring the rules: the documented last resort (unplace) is unchanged by them."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads
    from tests.test_section_day_run import _SLOTS

    scenario = _overload_scenario(
        f"t-r3 {rules}", blocked=[(day, s) for day in ("MON", "WED") for s in _SLOTS]
    )
    _inject(monkeypatch, _unavailable(_dr(), "MON") if rules else EMPTY_AVAILABILITY)

    report = repair_instructor_daily_overloads(scenario.id)

    assert [row["section"] for row in report["unplaced"]] == ["C1 S1"]
    assert "availability_blocked" not in report


@pytest.mark.django_db(transaction=True)
@RULES_OR_EMPTY
def test_clash_repair_never_moves_a_session_into_a_forbidden_day(
    repair_flags, monkeypatch, rules
) -> None:
    """T-R4 (single move): C1's clashing THU session goes to WED, not MON, which
    Dr Run may not teach (control: an empty availability takes MON)."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

    scenario = _clash_scenario(f"t-r4 {rules}")
    _inject(monkeypatch, _unavailable(_dr(), "MON") if rules else EMPTY_AVAILABILITY)

    report = repair_instructor_clashes(scenario.id)

    assert report["remaining_clashes"] == 0
    assert _course_days(scenario, "C1") == (
        ["SUN", "TUE", "WED"] if rules else ["SUN", "MON", "TUE"]
    )


@pytest.mark.django_db(transaction=True)
def test_clash_repair_leaves_a_clash_rather_than_break_a_rule(repair_flags, monkeypatch) -> None:
    """Owner decision G.3: with MON and WED both forbidden the clash stays,
    reported as before, and nothing is unplaced."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

    scenario = _clash_scenario("t-r4 left")
    _inject(monkeypatch, _unavailable(_dr(), "MON", "WED"))

    report = repair_instructor_clashes(scenario.id)

    assert report["remaining_clashes"] == 1 and report["unplaced"] == []
    assert report["locked_blocked"][-1]["reason"] == "no_clash_free_slot_preserving_duration"
    assert _course_days(scenario, "C1") == ["SUN", "TUE", "THU"]


@pytest.mark.django_db(transaction=True)
@RULES_OR_EMPTY
def test_the_instructor_first_safe_search_never_picks_a_forbidden_day(
    repair_flags, monkeypatch, rules
) -> None:
    """T-R5: the coordinated safe search (priority on) has two zero-cost moves for
    C1's THU session, MON and WED; Dr Run may not teach on MON (control: an empty
    availability takes MON)."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

    repair_flags.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    scenario = _safe_search_scenario(f"t-r5 {rules}")
    _inject(monkeypatch, _unavailable(_dr(), "MON") if rules else EMPTY_AVAILABILITY)

    report = repair_instructor_clashes(scenario.id)

    assert [r["instructor_safe"] for r in report["repaired"]] == [True]
    assert _course_days(scenario, "C1") == (
        ["SUN", "TUE", "WED"] if rules else ["SUN", "MON", "TUE"]
    )


@pytest.fixture
def compaction_flags(repair_flags):
    repair_flags.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    repair_flags.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 4
    repair_flags.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0
    return repair_flags


@pytest.mark.django_db(transaction=True)
@RULES_OR_EMPTY
def test_compaction_never_relocates_a_session_into_a_forbidden_day(
    compaction_flags, monkeypatch, rules
) -> None:
    """T-R6: closing C1's THU hole, compaction takes WED instead of MON, which
    Dr Run may not teach (control: an empty availability takes MON)."""
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _compaction_scenario(f"t-r6 {rules}")
    _inject(monkeypatch, _unavailable(_dr(), "MON") if rules else EMPTY_AVAILABILITY)

    report = compact_instructor_schedules(scenario.id)

    assert report["search"]["moves_accepted"] >= 1
    days = _course_days(scenario, "C1")
    assert "THU" not in days
    assert ("WED" if rules else "MON") in days and ("MON" in days) is not rules


@pytest.mark.django_db
@RULES_OR_EMPTY
def test_a_vacated_days_sessions_never_land_in_forbidden_cells(
    settings, monkeypatch, rules
) -> None:
    """T-R7: the day trade empties a two-session day by moving both sessions to the
    other days' afternoons; Dr Days may not teach afternoons, so no day is vacated
    (control: an empty availability vacates one)."""
    from core.services.timetable_instructor_compaction import compact_instructor_schedules
    from tests.test_instructor_day_trade import _THREE_DAYS, _board, _trade_on

    for name, value in {
        "TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED": True,
        "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED": True,
        "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
        "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED": True,
        "TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED": True,
        "TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS": 6,
        "TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS": 0,
    }.items():
        setattr(settings, name, value)
    _trade_on(settings)
    scenario = _board(_THREE_DAYS)
    afternoons = _av(
        (_dr("Dr Days"), KIND_UNAVAILABLE, ("SUN", "MON", "TUE", "WED", "THU"), 13 * 60, 24 * 60)
    )
    _inject(monkeypatch, afternoons if rules else EMPTY_AVAILABILITY)

    report = compact_instructor_schedules(scenario.id)

    assert report["instructor_impact"]["working_days_after"] == (3 if rules else 2)
    starts = set(
        SectionPlacement.objects.filter(board__scenario=scenario).values_list(
            "start_time", flat=True
        )
    )
    assert (starts <= {"09:00", "10:30"}) is rules


@pytest.mark.django_db(transaction=True)
@RULES_OR_EMPTY
def test_a_clash_repairing_bundle_switch_into_forbidden_mornings_is_refused(
    repair_flags, monkeypatch, rules
) -> None:
    """T-F1: ENG101's afternoon bundle clashes with C2 for Dr Run; the morning
    bundle would repair it, but he may not teach mornings (control: an empty
    availability switches)."""
    from core.services.timetable_fixed_pattern_search import improve_fixed_course_patterns

    pm = ("13:00", "16:00")
    scenario = _instructor_board(
        f"t-f1 {rules}",
        [
            ("C2", "S1", [("SUN", "12:00", "13:15")]),  # right after the morning bundle
            ("ENG101", "S1", [(day, *pm) for day in ("SUN", "MON", "WED", "THU")]),
        ],
    )
    _inject(monkeypatch, _no_mornings_for(_dr()) if rules else EMPTY_AVAILABILITY)

    report = improve_fixed_course_patterns(scenario.id, repair_clashes=True)

    starts = set(
        SectionPlacement.objects.filter(
            board__scenario=scenario, term_section__course_code="ENG101"
        ).values_list("start_time", flat=True)
    )
    assert report["evaluated"] >= 1
    assert starts == ({"13:00"} if rules else {"09:00"})
    assert report["accepted"] == (0 if rules else 1)


def _no_mornings_for(person: int):
    return _av((person, KIND_UNAVAILABLE, ("SUN", "MON", "TUE", "WED", "THU"), 0, 13 * 60))


# ── C.13: secondary recovery (kernel) and its persisted guard ────────

CORE_PM = _pat("core_pm", [(SUN, PM)])
SOFT_PM = _pat("soft_pm", [(SUN, PM)])
SOFT_AM = _pat("soft_am", [(SUN, AM)])


@RULES_OR_EMPTY
def test_secondary_recovery_never_moves_a_section_into_a_forbidden_pattern(settings, rules) -> None:
    """T-R8: SOFT clashes with the locked CORE; the only pattern that frees it is a
    morning teacher 7 may not teach (control: an empty availability takes it)."""
    from core.services.timetable_secondary_recovery import recover_secondary_quality

    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True
    sections = {"CORE": _sec("CORE", CORE_PM), "SOFT": _sec("SOFT", SOFT_PM)}
    profiles = {"s": StudentProfile("s", "CS", ["CORE", "SOFT"], RiskTier.C, 0)}
    tiers = {"CORE": "T1", "SOFT": "T3"}
    ids = {"SOFT": frozenset({7})}
    baseline = evaluate_generated_timetable_candidate(
        "before", list(sections.values()), profiles, {}, ids, tiers
    )

    report = recover_secondary_quality(
        current_eval=baseline,
        sections_by_id=sections,
        pattern_catalog={"fam": [SOFT_PM, SOFT_AM]},
        student_profiles=profiles,
        course_rigidity={},
        room_evaluator=lambda states: {
            "unassigned_count": 0,
            "unassigned_section_ids": [],
            "proven_optimal": True,
        },
        section_instructor_ids=ids,
        course_tiers=tiers,
        locked_section_ids={"CORE"},
        time_limit_seconds=10,
        max_iterations=3,
        instructor_availability=NO_MORNINGS if rules else EMPTY_AVAILABILITY,
    )["report"]

    assert bool(report["applied"]) is not rules
    if rules:
        assert report["structural_rejections"] > 0


@pytest.mark.django_db
@pytest.mark.parametrize("rules", [True, False], ids=["rules", "no-rule-control"])
def test_the_recovery_runner_rolls_back_a_persisted_board_with_a_new_violation(
    settings, monkeypatch, rules
) -> None:
    """T-R9: with the search's own reject taken out, it moves SOFT from SUN 09:00
    (clashing with the locked CORE) to SUN 10:30, which teacher 7 may not teach;
    the persisted guard rolls the pass back (control: without the rule it is kept)."""
    from core.services import timetable_secondary_recovery as recovery
    from core.services import timetable_secondary_recovery_runner as runner

    scenario, soft = _recovery_scenario(settings, monkeypatch)
    late_morning = _av((7, KIND_UNAVAILABLE, ("SUN",), 10 * 60 + 30, 12 * 60))
    _inject(monkeypatch, late_morning if rules else EMPTY_AVAILABILITY)
    monkeypatch.setattr(recovery, "move_adds_availability_violation", lambda *a, **k: False)
    profiles = {"s": StudentProfile("s", "CS", ["CORE", "SOFT"], RiskTier.C, 0)}

    report = runner.run_secondary_recovery_for_scenario(
        scenario.id,
        {},
        profiles,
        {},
        {"SOFT_M1": frozenset({7})},
        {"CORE": "T1", "SOFT": "T3"},
    )

    soft.refresh_from_db()
    if rules:
        assert not report["applied"]
        assert report["stop_reason"] == "persistence_guard_rejected"
        assert [f["metric"] for f in report["regressions"]] == [
            "instructor_availability_violations"
        ]
        assert soft.start_time == "09:00"
    else:
        assert report["applied"]
        assert soft.start_time == "10:30"


@pytest.mark.django_db
def test_the_recovery_runner_hands_the_search_the_rules(settings, monkeypatch) -> None:
    """C.13 wiring: with the search's own reject in place the runner's rules reach
    it, so SOFT is never moved into SUN 10:30 and the guard has nothing to roll
    back (the same board with the reject taken out is rolled back, above); the
    catalogue is built with the rules and the map too."""
    from core.services import timetable_optimizer_v2 as v2
    from core.services import timetable_secondary_recovery_runner as runner

    scenario, soft = _recovery_scenario(settings, monkeypatch)
    rules = _av((7, KIND_UNAVAILABLE, ("SUN",), 10 * 60 + 30, 12 * 60))
    _inject(monkeypatch, rules)
    stub, asked = v2._build_pattern_catalog_for_scenario, []

    def catalogue(scenario_id, **kwargs):
        asked.append(kwargs)
        return stub(scenario_id, **kwargs)

    monkeypatch.setattr(v2, "_build_pattern_catalog_for_scenario", catalogue)
    profiles = {"s": StudentProfile("s", "CS", ["CORE", "SOFT"], RiskTier.C, 0)}

    report = runner.run_secondary_recovery_for_scenario(
        scenario.id, {}, profiles, {}, {"SOFT_M1": frozenset({7})}, {"CORE": "T1", "SOFT": "T3"}
    )

    soft.refresh_from_db()
    assert not report["applied"]
    assert report["stop_reason"] != "persistence_guard_rejected"
    assert soft.start_time == "09:00"
    assert asked == [{"availability": rules, "section_instructor_ids": {"SOFT_M1": frozenset({7})}}]


def _recovery_scenario(settings, monkeypatch):
    """The recovery runner's precedent board: CORE (locked) and SOFT both on SUN
    09:00 (a clash for the one student); SUN 10:30 frees SOFT. Returns
    ``(scenario, SOFT's placement)``."""
    from core.models import (
        Room,
        ScenarioSectionBudget,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = True
    settings.TIMETABLE_SECONDARY_RECOVERY_SECONDS = 2
    settings.TIMETABLE_SECONDARY_RECOVERY_ITERATIONS = 2
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="recovery av", gender="M", programs=["CS"]
    )
    delivery = DeliveryBoard.objects.create(
        scenario=scenario, program="CS", nominal_term=1, label="CS"
    )
    Room.objects.create(
        room_code="R1", capacity=40, room_type="lecture", section="M", department="CS"
    )
    rows = {}
    for course, start, end, locked in [
        ("CORE", "09:00", "10:15", True),
        ("SOFT", "09:00", "10:15", False),
    ]:
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=course,
            course_code=course,
            department="CS",
            credit_hours=3,
            total_demand=5,
            planned_sections=1,
            max_per_section=30,
            programme_term=1,
        )
        section = TermSection.objects.create(
            scenario=scenario,
            course_key=course,
            course_code=course,
            section="M1",
            available_capacity=30,
        )
        rows[course] = SectionPlacement.objects.create(
            board=delivery,
            term_section=section,
            day="SUN",
            start_time=start,
            end_time=end,
            room="R1" if locked else "",
            is_locked=locked,
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day="SUN",
            start_time=start,
            end_time=end,
            room="R1" if locked else "",
            instructor="",
        )
    states = {s.section_id: s for s in build_section_states_for_scenario(scenario.id)}
    family = states["SOFT_M1"].pattern_family
    patterns = [
        CanonicalPattern(
            name, "", [SectionMeeting(SUN, *cell)], family, "75", False, 1, frozenset({SUN}), ""
        )
        for name, cell in (("soft_nine", (540, 615)), ("soft_late", (630, 705)))
    ]
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2._build_pattern_catalog_for_scenario",
        lambda _sid, **_kwargs: {family: patterns},
    )
    return scenario, rows["SOFT"]


def test_the_repair_guard_spares_only_a_meeting_online_on_every_board() -> None:
    """H7 in the cap and clash repairs: a meeting whose copies are online on every
    board is never bound; one campus on some board is (control: no rule, no guard)."""
    from types import SimpleNamespace

    from core.services.timetable_instructor_cap_repair import _availability_guard

    class Lookup:
        def __init__(self, online_boards):
            self.online_boards = online_boards

        def is_online_course_for_board(self, board, _code):
            return board in self.online_boards

    section = SimpleNamespace(course_key="GS101", course_code="GS101", course_number="101")
    here = SimpleNamespace(pk=1, board="AI", term_section=section)
    copies = {1: [here, SimpleNamespace(pk=2, board="DS", term_section=section)]}

    def teachers(_placement):
        return frozenset({7})

    everywhere = _availability_guard(NO_MORNINGS, Lookup({"AI", "DS"}), copies, teachers)
    assert not everywhere(here, "SUN", "09:00", "10:15")
    one_board = _availability_guard(NO_MORNINGS, Lookup({"AI"}), copies, teachers)
    assert one_board(here, "SUN", "09:00", "10:15")
    assert not one_board(here, "SUN", "13:00", "14:15")
    assert _availability_guard(EMPTY_AVAILABILITY, Lookup(set()), copies, teachers) is None
    # D-A: online on both boards it sits on, but its course is a campus course on
    # a board of its term it is not placed on yet: bound. Online on every board of
    # its term too: exempt. Asked by the section's planner course key.
    asked: list[str] = []

    def course_online(online: bool):
        def ask(course_key: str) -> bool:
            asked.append(course_key)
            return online

        return ask

    campus_term = _availability_guard(
        NO_MORNINGS, Lookup({"AI", "DS"}), copies, teachers, course_online=course_online(False)
    )
    assert campus_term(here, "SUN", "09:00", "10:15")
    online_term = _availability_guard(
        NO_MORNINGS, Lookup({"AI", "DS"}), copies, teachers, course_online=course_online(True)
    )
    assert not online_term(here, "SUN", "09:00", "10:15")
    assert asked == ["GS101", "GS101"]


def _reads(ctx, table: str) -> int:
    return sum(1 for q in ctx.captured_queries if f'FROM "{table}"' in q["sql"])


@pytest.mark.django_db
def test_the_online_exemption_reads_its_static_half_once_per_scope(av_on) -> None:
    """identity:F6: inside one scope the boards, the budgets and the per-programme
    online lookups are read once; each call re-reads only the placements (one
    query). Control: unscoped, every call builds them again."""
    sc, ai_board, ds_board, key, _people = _two_boards("cost", ds_online=False)
    ts = ots.section(sc, key, "S1")
    for board in (ai_board, ds_board):
        ots.place(board, ts, AFTERNOON)

    def three_calls() -> None:
        assert helper.online_exempt_sections(sc.id) == frozenset()
        assert helper.placed_online_flags(sc.id) == {ts.pk: (False, True)}
        assert helper.online_exempt_sections(sc.id) == frozenset()

    with CaptureQueriesContext(connection) as scoped:
        with helper.availability_scope(sc.id):
            three_calls()
    with CaptureQueriesContext(connection) as unscoped:
        three_calls()

    static = ("programme_requirements", "delivery_boards", "scenario_section_budgets")
    assert _reads(scoped, "section_placements") == 3
    assert {table: _reads(scoped, table) for table in static} == {
        "programme_requirements": 2,  # one per programme list (AI, DS)
        "delivery_boards": 1,
        "scenario_section_budgets": 1,
    }
    assert {table: _reads(unscoped, table) for table in static} == {
        "programme_requirements": 6,
        "delivery_boards": 2,
        "scenario_section_budgets": 2,
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "repair", ["repair_instructor_daily_overloads", "repair_instructor_clashes"]
)
@pytest.mark.parametrize("ds_online", [True, False], ids=["online-everywhere", "campus-on-ds"])
def test_the_cap_and_clash_repairs_ask_the_one_online_exemption(
    av_on, monkeypatch, repair, ds_online
) -> None:
    """D-A wiring: GS101 S1 sits only on the AI board, where it is online; the
    repairs' guard binds it unless its course is online on the DS board of its
    term too (the scope's ``online_exemption`` is handed in)."""
    from core.models import SectionPlacement
    from core.services import timetable_instructor_cap_repair as cap_repair

    sc, ai_board, key, a = _online_here(elsewhere=False, ds_online=ds_online)
    ots.place(ai_board, ots.section(sc, key, "S1"), AFTERNOON)
    _engine_row(sc, key, "S1", a)
    real, guards = cap_repair._availability_guard, []

    def spy(*args, **kwargs):
        guards.append(real(*args, **kwargs))
        return guards[-1]

    monkeypatch.setattr(cap_repair, "_availability_guard", spy)
    getattr(cap_repair, repair)(sc.id)

    (guard,) = guards
    placement = SectionPlacement.objects.select_related("board", "term_section").filter(
        board=ai_board
    )[0]
    assert guard(placement, "SUN", "09:00", "10:15") is not ds_online
    assert guard(placement, "SUN", "13:00", "14:15") is False


# ── T-O1: a whole Generate run ────────────────────────────────────────


def _board_violations(scenario) -> tuple[int, dict]:
    """Forbidden meetings on the persisted board under the map in force (B.8), and
    the busiest charged teacher's first meeting ``{"name", "day"}``."""
    from core.services.instructor_availability import load_instructor_availability
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    states = {s.section_id: s for s in build_section_states_for_scenario(scenario.id)}
    charged = sta.load_assignment(scenario.id, incomplete="collect").v2_map()
    exempt = [f"{key}_{label}" for key, label in helper.online_exempt_sections(scenario.id)]
    teachers = sorted({iid for ids in charged.values() for iid in ids})
    rules = load_instructor_availability(teachers)
    count = constraints.count_instructor_availability_violations(
        states, charged, rules, exempt=exempt
    )
    load: dict[int, list] = {}
    for sid, ids in charged.items():
        for iid in ids:
            load.setdefault(iid, []).extend(states[sid].meetings if sid in states else [])
    busiest = max(sorted(load), key=lambda iid: len(load[iid]))
    first = min(load[busiest], key=lambda m: (m.day, m.start_min))
    name = Instructor.objects.get(pk=busiest).full_name
    return count, {"name": name, "day": ("SUN", "MON", "TUE", "WED", "THU")[first.day]}


def _generate(settings, flag: bool, rule_for: dict | None) -> tuple[int, dict]:
    from core.services.timetable_run_seed import seeded_timetable_run
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from tests.test_one_teacher_flag_parity import build_corpus

    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    out: dict = {}
    try:
        with transaction.atomic():
            scenario = build_corpus(11)
            if rule_for is not None:
                person = Instructor.objects.get(full_name=rule_for["name"])
                rule(person, KIND_UNAVAILABLE, (rule_for["day"],))
            with seeded_timetable_run(3):
                result = run_v2_optimisation_guarded(
                    scenario.id,
                    mode="full",
                    strategies=["compact"],
                    max_iterations=2,
                    run_chain=False,
                    run_cpsat=False,
                )
            assert "error" not in result, result.get("error")
            out["violations"], out["busiest"] = _board_violations(scenario)
            raise _Rollback
    except _Rollback:
        pass
    return out["violations"], out["busiest"]


@pytest.mark.django_db
def test_a_generate_run_leaves_no_forbidden_meeting_on_the_persisted_board(av_on) -> None:
    """T-O1: the busiest charged teacher of a flag-off Generate may not teach on the
    day of his first meeting. With the flag off the same run (control, asserted
    first) breaks that rule; with it on, the persisted board breaks none."""
    _count, busiest = _generate(av_on, False, None)

    control, _ = _generate(av_on, False, busiest)
    assert control >= 1
    ruled, _ = _generate(av_on, True, busiest)
    assert ruled == 0


# ── C.19 / C.20: the server-side semi-automatic writers ───────────────

FLAG_OR_OFF = pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])


def _candidate(day: str, start: str, end: str) -> dict:
    """A clean, improving slot candidate as the slot preview reports one."""
    return {
        "day": day,
        "start": start,
        "end": end,
        "tone": "clean",
        "critical_count": 0,
        "warning_count": 0,
        "impact_improvement": 1,
    }


def _bulk_feed(monkeypatch, placement, candidates) -> None:
    """The bulk fixer is offered ``placement`` (from a student clash action) and,
    for it, exactly ``candidates``."""
    from core.services import timetable_workspace as ws

    monkeypatch.setattr(
        ws,
        "build_scenario_builder_actions",
        lambda *_a, **_k: {
            "actions": [
                {
                    "kind": "student_time_clash",
                    "board_id": placement.board_id,
                    "placement_ids": [placement.id],
                }
            ]
        },
    )
    monkeypatch.setattr(
        ws, "preview_placement_slot_candidates", lambda *_a, **_k: {"candidates": candidates}
    )


def _sunday(sc, key: str):
    from core.models import SectionPlacement

    return SectionPlacement.objects.select_related("board", "term_section").get(
        board__scenario=sc, term_section__course_key=key, day="SUN"
    )


@pytest.mark.django_db
@FLAG_OR_OFF
def test_the_bulk_fixer_never_proposes_a_slot_the_teacher_may_not_teach(
    av_on, monkeypatch, flag
) -> None:
    """T-W1 (preview): of two clean candidates the first, MON 09:00, is a morning
    Dr A may not teach; the fixer proposes MON 14:30 (control: flag off, 09:00)."""
    from core.services.timetable_workspace import preview_bulk_safe_time_moves

    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _board, key, _a = _placed_ruled_board(f"t-w1 {flag}")
    _bulk_feed(
        monkeypatch,
        _sunday(sc, key),
        [_candidate("MON", "09:00", "10:15"), _candidate("MON", "14:30", "15:45")],
    )

    preview = preview_bulk_safe_time_moves(sc.id)

    assert [(m["new_day"], m["new_start"]) for m in preview["moves"]] == [
        ("MON", "14:30" if flag else "09:00")
    ]


@pytest.mark.django_db
@FLAG_OR_OFF
def test_the_bulk_fixer_re_checks_the_rules_when_it_applies(av_on, monkeypatch, flag) -> None:
    """T-W1 (apply): a previewed move into MON 09:00 (the preview stubbed, as if
    the rule arrived after it) is skipped as "instructor_unavailable"
    (control: flag off, it is applied)."""
    from core.services import timetable_workspace as ws

    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _board, key, _a = _placed_ruled_board(f"t-w1 apply {flag}")
    sunday = _sunday(sc, key)
    _bulk_feed(monkeypatch, sunday, [_candidate("MON", "09:00", "10:15")])
    move = {
        "placement_id": sunday.id,
        "board_id": sunday.board_id,
        "label": "CS101-S1",
        "new_day": "MON",
        "new_start": "09:00",
        "new_end": "10:15",
    }
    monkeypatch.setattr(
        ws, "preview_bulk_safe_time_moves", lambda *_a, **_k: {"moves": [move], "skipped": []}
    )

    result = ws.apply_bulk_safe_time_moves(sc.id)

    sunday_after = _placed_meetings(sc, key)
    if flag:
        assert result["applied_count"] == 0
        assert [row["reason"] for row in result["skipped"]] == ["instructor_unavailable"]
        assert ("SUN", "13:00", "14:15") in sunday_after
    else:
        assert result["applied_count"] == 1
        assert ("MON", "09:00", "10:15") in sunday_after


def _english_board(name: str):
    """ENG101 S1 on the morning bundle (SUN, MON, WED, THU 09:00-12:00), its engine
    teacher Dr A may not teach THU afternoons."""
    sc, board, key, (a,) = _greedy_scenario(name, code="ENG101")
    ts = ots.section(sc, key, "S1")
    ots.place(board, ts, [(day, "09:00", "12:00") for day in ("SUN", "MON", "WED", "THU")])
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_UNAVAILABLE, ("THU",), "13:00", "16:00")
    return sc, key, a


@pytest.mark.django_db
@FLAG_OR_OFF
def test_an_english_switch_is_checked_on_every_meeting_of_its_new_bundle(
    av_on, monkeypatch, flag
) -> None:
    """T-W2: the SUN anchor's afternoon candidate is legal, but the afternoon
    bundle also moves THU, which Dr A may not teach: skipped (control: flag off,
    proposed)."""
    from core.services.timetable_workspace import preview_bulk_safe_time_moves

    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, key, a = _english_board(f"t-w2 {flag}")
    sunday = _sunday(sc, key)
    _bulk_feed(monkeypatch, sunday, [_candidate("SUN", "13:00", "16:00")])

    preview = preview_bulk_safe_time_moves(sc.id)

    assert len(preview["moves"]) == (0 if flag else 1)
    if flag:
        checker = helper.section_rule_checker(sc.id)
        assert checker.blocked(sunday, "SUN", "13:00", "16:00") == (a.pk,)
        assert checker.blocked(sunday, "SUN", "09:00", "12:00") == ()


@pytest.mark.django_db
@ONLINE_CASES
def test_the_rule_checker_spares_only_a_section_online_on_every_board(
    av_on, elsewhere, ds_online
) -> None:
    """H7 (D-A) for the writers: GS101 S1 is online on the AI board; online on the
    DS board of its term too, its teacher's "no mornings" never binds it. A campus
    course on the DS board, placed there or not yet, it does."""
    from core.models import TermSection

    sc, ai_board, key, a = _online_here(elsewhere=elsewhere, ds_online=ds_online)
    ts = TermSection.objects.filter(scenario=sc, course_key=key, section="S1").first()
    ts = ts or ots.section(sc, key, "S1")
    ots.place(ai_board, ts, [("SUN", "18:00", "19:15")])
    _engine_row(sc, key, "S1", a)
    placement = SectionPlacement.objects.select_related("board", "term_section").get(
        board=ai_board, term_section=ts
    )

    blocked = helper.section_rule_checker(sc.id).blocked(placement, "MON", "09:00", "10:15")

    assert blocked == (() if ds_online else (a.pk,))


def _move_set(placement, day: str, start: str, end: str) -> list[dict]:
    from core.services.timetable_repair import _hard_feasibility_rejections_for_move_set

    return _hard_feasibility_rejections_for_move_set(
        placements_by_id={placement.id: placement},
        moves=[
            {
                "placement_id": placement.id,
                "day": day,
                "start": start,
                "end": end,
                "room": "",
                "room_reasons": [],
            }
        ],
        planning_scope=None,
    )


@pytest.mark.django_db
@FLAG_OR_OFF
def test_registration_repair_rejects_a_destination_the_teacher_may_not_teach(av_on, flag) -> None:
    """T-W3: the repair's hard feasibility check rejects MON 09:00 for a "no
    mornings" teacher with its own code, never folded into the critical count,
    and lets MON 14:30 through (control: flag off, no such rejection)."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _board, key, a = _placed_ruled_board(f"t-w3 {flag}")
    sunday = _sunday(sc, key)

    morning = _move_set(sunday, "MON", "09:00", "10:15")
    afternoon = _move_set(sunday, "MON", "14:30", "15:45")

    codes = [reason["code"] for reason in morning]
    assert ("INSTRUCTOR_UNAVAILABLE" in codes) is flag
    assert "TIME_OR_INSTRUCTOR_CONFLICT" not in codes
    if flag:
        (reason,) = [r for r in morning if r["code"] == "INSTRUCTOR_UNAVAILABLE"]
        assert reason["details"]["instructor_ids"] == [a.pk]
        assert reason["details"]["placement_id"] == sunday.id
    assert "INSTRUCTOR_UNAVAILABLE" not in [reason["code"] for reason in afternoon]


@pytest.mark.django_db
def test_apply_time_re_validation_sees_a_rule_added_after_the_analysis(av_on) -> None:
    """T-W4: the move set accepted under the teacher's rules at analysis time (he
    may not teach THU) is refused once a "no mornings" rule is saved: each
    apply-time check reads the rules afresh."""
    sc, board, key, (a,) = _greedy_scenario("t-w4")
    ots.place(board, ots.section(sc, key, "S1"), AFTERNOON)
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_UNAVAILABLE, ("THU",))
    sunday = _sunday(sc, key)

    assert "INSTRUCTOR_UNAVAILABLE" not in [
        r["code"] for r in _move_set(sunday, "MON", "09:00", "10:15")
    ]
    rule(a, KIND_UNAVAILABLE, end="13:00")
    assert "INSTRUCTOR_UNAVAILABLE" in [
        r["code"] for r in _move_set(sunday, "MON", "09:00", "10:15")
    ]


@pytest.mark.django_db
@FLAG_OR_OFF
def test_a_rule_edit_changes_the_repair_analysis_fingerprint(av_on, flag) -> None:
    """T-W7 (H5): a saved rule for the section's teacher invalidates cached repair
    analyses (control: flag off, the fingerprint ignores the rules; with no rule it
    is the flag-off fingerprint either way)."""
    from core.services.timetable_repair import _repair_state_fingerprint

    sc, board, key, (a,) = _greedy_scenario(f"t-w7 {flag}")
    ots.place(board, ots.section(sc, key, "S1"), AFTERNOON)
    _engine_row(sc, key, "S1", a)
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    off = _repair_state_fingerprint(sc.id, [])
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    before = _repair_state_fingerprint(sc.id, [])

    rule(a, KIND_UNAVAILABLE, end="13:00")
    after = _repair_state_fingerprint(sc.id, [])

    assert before == off
    assert (after != before) is flag


# ── D + H10: a rule on a teacher the run never charges ────────────────


def _uncharged_rule() -> None:
    """Dr Fahd may teach AI124 (AI2) but the corpus charges Dr Amal with every
    AI124 section: a rule for him binds no charged section (H10)."""
    rule(Instructor.objects.get(full_name="Dr Fahd"), KIND_UNAVAILABLE)


@pytest.mark.django_db
def test_a_rule_on_a_never_charged_teacher_leaves_the_board_and_the_catalogue_alone(
    day_spread_golden_flags,
) -> None:
    """H10 / D.1: Optimise Current persists the very board of the flag-off run, and
    the catalogue built for the run's map is the plain one (additive report keys
    may appear: the rule binds a teacher of the universe)."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_optimizer_v2 import (
        _build_pattern_catalog_for_scenario,
        _catalog_rules,
    )
    from tests.test_one_teacher_flag_parity import build_corpus

    off_board, _keys = _current_run(
        day_spread_golden_flags, False, _no_rules, after_corpus=_uncharged_rule
    )
    on_board, _keys = _current_run(
        day_spread_golden_flags, True, _no_rules, after_corpus=_uncharged_rule
    )
    assert on_board == off_board

    scenario = build_corpus(11)
    _uncharged_rule()
    auto_place_scenario(scenario.id, strategy="compact")
    charged = sta.load_assignment(scenario.id, incomplete="collect").v2_map()
    availability = helper.availability_for(scenario.id)
    assert availability  # the rule IS loaded: Dr Fahd is in the teacher universe
    assert _build_pattern_catalog_for_scenario(
        scenario.id, **_catalog_rules(availability, charged)
    ) == _build_pattern_catalog_for_scenario(scenario.id)


# ── T-P3 / T-P5: one rule read per scope; the universe never grows ─────


@pytest.mark.django_db
@pytest.mark.parametrize("rows", [False, True], ids=["zero-rows", "rules"])
def test_every_p1_path_reads_the_rules_at_most_once_per_scope(av_on, rows) -> None:
    """T-P3: inside one request scope every P1 path issues at most one rule query,
    and the run entry points (the placer, the guarded runner) open their own."""
    from tests.test_one_teacher_flag_parity import LINKS, build_corpus

    scenario = build_corpus()
    if rows:
        for person in Instructor.objects.filter(full_name__in=set(LINKS.values())):
            rule(person, KIND_UNAVAILABLE, ("THU",), "16:00", "17:00")

    scoped, unscoped = {}, {}
    for name, step in _p1_steps(scenario).items():
        with CaptureQueriesContext(connection) as ctx, sta.request_scope():
            step()
        scoped[name] = len(rule_queries(ctx.captured_queries))
    for name in ("auto_place_scenario", "runner_current"):
        with CaptureQueriesContext(connection) as ctx:
            _p1_steps(scenario)[name]()
        unscoped[name] = len(rule_queries(ctx.captured_queries))

    assert max(scoped.values()) <= 1, scoped
    assert unscoped == {"auto_place_scenario": 1, "runner_current": 1}


@pytest.mark.django_db
def test_the_teacher_universe_never_grows_inside_a_run(av_on) -> None:
    """T-P5 (B.4): the rules are loaded for the universe of the first read in a
    scope; at the end of a Generate and an Optimise Current run the universe is a
    subset of it, so no teacher appears unchecked mid-run."""
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from tests.test_one_teacher_flag_parity import build_corpus

    scenario = build_corpus(11)
    first = sta.load_assignment(scenario.id, incomplete="collect").universe
    assert first

    for mode in ("full", "current"):
        result = run_v2_optimisation_guarded(
            scenario.id,
            mode=mode,
            strategies=["compact"],
            max_iterations=1,
            run_chain=False,
            run_cpsat=False,
        )
        assert "error" not in result, result.get("error")
        assert sta.load_assignment(scenario.id, incomplete="collect").universe <= first


# ── Structural allow-lists ─────────────────────────────────────────────

AV_FLAG_READS = re.compile(
    r"is_instructor_availability_enabled|INSTRUCTOR_AVAILABILITY_SETTING"
    r"|settings\.TIMETABLE_INSTRUCTOR_AVAILABILITY|[\"']TIMETABLE_INSTRUCTOR_AVAILABILITY[\"']"
)
ASKS = re.compile(r"\bavailability_for\(|\bsection_rule_checker\(")
KERNELS = re.compile(r"\bmove_adds_availability_violation\(")
HELPER_REFERENCE = re.compile(rf"{ASKS.pattern}|{KERNELS.pattern}|\binstructor_availability\b")


def _av_sources_matching(pattern: re.Pattern) -> set[str]:
    found = set()
    for root in ("config", "core", "scheduler"):
        for path in (REPO / root).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                found.add(path.relative_to(REPO).as_posix())
    return found


def test_only_the_flags_module_and_the_helper_read_the_flag() -> None:
    """Every stage asks the helper (``rules_enabled`` / ``availability_for``),
    never the flag; docs and comments may name it."""
    assert _av_sources_matching(AV_FLAG_READS) == {
        "config/settings.py",
        "core/services/timetable_flags.py",
        "core/services/timetable_instructor_availability.py",
    }


def test_only_the_automatic_stages_and_the_semi_automatic_writers_ask_for_the_rules() -> None:
    """Manual moves, validation, the previews, the views, exports and the
    scheduler package never ask (B2 adds the runner and the backstop)."""
    assert _av_sources_matching(ASKS) == {
        "core/services/timetable_instructor_availability.py",
        "core/services/timetable_autoplace.py",
        "core/services/timetable_solver.py",
        "core/services/timetable_local_search.py",
        "core/services/timetable_load_balanced.py",
        "core/services/timetable_optimizer_v2.py",
        "core/services/timetable_secondary_recovery_runner.py",
        "core/services/timetable_instructor_cap_repair.py",
        "core/services/timetable_instructor_compaction.py",
        "core/services/timetable_fixed_pattern_search.py",
        "core/services/section_teacher_assignment.py",
        "core/services/timetable_workspace.py",
        "core/services/timetable_repair.py",
    }


def test_only_the_in_memory_searches_use_the_move_delta() -> None:
    assert _av_sources_matching(KERNELS) == {
        "core/services/timetable_constraints.py",
        "core/services/timetable_local_search_v2.py",
        "core/services/timetable_local_search_chains.py",
        "core/services/timetable_secondary_recovery.py",
    }


#: The day-spread rule's readers (every stage that chooses or moves a meeting's
#: time, ``tests/test_section_day_spread.py``) without the settings and flags
#: modules, plus the teacher choice and the whole-bundle search.
TIME_CHOOSERS = {
    "core/services/timetable_autoplace.py",
    "core/services/timetable_solver.py",
    "core/services/timetable_local_search.py",
    "core/services/timetable_local_search_v2.py",
    "core/services/timetable_local_search_chains.py",
    "core/services/timetable_load_balanced.py",
    "core/services/timetable_cpsat_polisher.py",
    "core/services/timetable_secondary_recovery.py",
    "core/services/timetable_instructor_cap_repair.py",
    "core/services/timetable_instructor_compaction.py",
    "core/services/section_teacher_assignment.py",
    "core/services/timetable_fixed_pattern_search.py",
}


def test_every_time_choosing_module_references_the_rules() -> None:
    """A module that chooses or moves a meeting's time must ask the helper (or
    take the run's availability); a new one turns this red until it does. The
    set is the day-spread rule's readers, so a stage added there is added here."""
    from tests.test_section_day_spread import ALLOWED_READERS

    assert ALLOWED_READERS - {"config/settings.py", "core/services/timetable_flags.py"} <= (
        TIME_CHOOSERS
    )
    missing = sorted(
        path
        for path in TIME_CHOOSERS
        if not HELPER_REFERENCE.search((REPO / path).read_text(encoding="utf-8"))
    )
    assert missing == []


#: Every door to the rules: asking for them, the switch, the hot-path read and
#: the loaders (identity:F8).
RULE_DOORS = frozenset(
    {
        "availability_for",
        "section_rule_checker",
        "rules_enabled",
        "bound_availability",
        "load_instructor_availability",
        "load_time_rules",
    }
)


def _functions_asking(path: str, doors: frozenset[str] = RULE_DOORS) -> set[str]:
    """Where ``path`` opens one of ``doors``: the top-level functions, the class
    methods (``"Class.method"``), a class body (``"Class"``) or the module body
    (``"<module>"``) holding a call to it, by name (``availability_for(...)``) or
    as an attribute (``_tia.availability_for(...)``)."""
    import ast

    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    askers: set[str] = set()

    def scan(node: ast.AST, name: str) -> None:
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if called in doors:
                askers.add(name)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            scan(node, node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    scan(item, f"{node.name}.{item.name}")
                else:
                    scan(item, node.name)
        else:
            scan(node, "<module>")
    return askers


def test_the_workspace_and_the_repair_ask_only_in_their_semi_automatic_writers() -> None:
    """I6: the manual move/create paths, validation and the previews that share
    these modules never open a door to the rules, whether they call it by name
    or as a module attribute, in a function or in a class method."""
    assert _functions_asking("core/services/timetable_workspace.py") == {
        "preview_bulk_safe_time_moves",
        "apply_bulk_safe_time_moves",
    }
    assert _functions_asking("core/services/timetable_repair.py") == {
        "_repair_state_fingerprint",
        "_move_set_rule_checker",
    }


#: Who may open the doors that are not a stage's ask (identity:F8): the switch,
#: the evaluator's no-load read and the loaders. The helper module and the
#: rules module itself are left out.
DOOR_CALLERS = {
    "rules_enabled": {
        "core/instructor_views.py::_time_rules_enforced",
        "core/services/section_teacher_assignment.py::_collect",
        "core/services/timetable_autoplace.py::auto_place_board",
        "core/services/timetable_repair.py::_move_set_rule_checker",
    },
    "bound_availability": {
        "core/services/timetable_candidate_eval.py::evaluate_generated_timetable_candidate",
    },
    "load_instructor_availability": set(),
    "load_time_rules": {"core/instructor_views.py::_time_rule_summaries"},  # the rules editor
}


def test_the_switch_the_hot_path_read_and_the_loaders_are_opened_only_where_pinned() -> None:
    own = {
        "core/services/timetable_instructor_availability.py",
        "core/services/instructor_availability.py",
    }
    found: dict[str, set[str]] = {door: set() for door in DOOR_CALLERS}
    for root in ("config", "core", "scheduler"):
        for path in (REPO / root).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if "migrations" in path.parts or rel in own:
                continue
            for door in DOOR_CALLERS:
                found[door] |= {
                    f"{rel}::{name}" for name in _functions_asking(rel, frozenset({door}))
                }
    assert found == DOOR_CALLERS
