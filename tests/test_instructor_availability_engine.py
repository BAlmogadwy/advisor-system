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
  picked for a section at a time his rules forbid; a charged violator is kept.

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


def _current_run(settings, flag: bool, rules) -> tuple[dict, set[str]]:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from tests.test_one_teacher_flag_parity import build_corpus, capture_board

    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    out: dict = {}
    try:
        with transaction.atomic():
            rules()
            scenario = build_corpus(11)
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
