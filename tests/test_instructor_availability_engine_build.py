"""``TIMETABLE_INSTRUCTOR_AVAILABILITY``: the stages that BUILD a board (B1 review,
fix plan decisions D-B, D-C, D-M, D-N, D-O and the D-P holes of the builders).

- D-C: the per-teacher option feed may miss a legal week; the greedy's tier 2 and
  the one-teacher assignment use the COMPLETE legal set;
- D-B: nothing is left unplaced because of a rule: an ENGINE teacher whose legal
  options are all taken is RELEASED for the run (charged to nobody), the section
  is placed, and the layout-aware re-pick after the build chooses its teacher;
- D-M: CP-SAT keeps a FIXED teacher without a legal week to his fewest
  forbidden meetings instead of solving him unpruned;
- D-N: hybrid and load_balanced keep the greedy boards' report;
- D-O: with the flag OFF no rule entry point runs and no report key appears.

Helpers come from ``tests/test_instructor_availability_engine.py`` (the slice's
main module). Every behavioural test has a control without the rule.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.models import DeliveryBoard, Instructor, ProgrammeRequirement, TermSectionMeeting
from core.services import section_teacher_assignment as sta
from core.services import timetable_instructor_availability as helper
from core.services.instructor_availability import KIND_AVAILABLE_ONLY, KIND_UNAVAILABLE
from tests import one_teacher_support as ots
from tests._instructor_availability_fixture import AVAILABILITY_FLAGS, rule
from tests.test_instructor_availability_engine import (
    _blocked,
    _board_result,
    _board_violations,
    _engine_map,
    _engine_row,
    _greedy,
    _greedy_scenario,
    _person,
    _pin,
    _placed_meetings,
    _ruled_board,
    _rules_of,
)


@pytest.fixture
def av_on(settings):
    for name, value in AVAILABILITY_FLAGS.items():
        setattr(settings, name, value)
    return settings


# ── D-C: the complete legal set ───────────────────────────────────────────


def _split_windows(name: str, *, pinned: bool):
    """construction:CON-2's fixture. Dr A may teach only SUN 09:00-11:45 and TUE
    13:00-15:45; his pinned CS200 S1 holds SUN 09:00 + TUE 13:00. Every week the
    same-index feed generates for CS101 (SUN 09:00 or 10:30 with TUE 13:00, SUN
    09:00 with TUE 13:00 or 14:30) clashes with CS200; SUN 10:30 + TUE 14:30 is
    legal and free, and only the complete set holds it."""
    from core.services.course_identity import planner_course_key

    sc, board, key, (a,) = _greedy_scenario(name, pin=pinned)
    ProgrammeRequirement.objects.create(
        program="AI", course_code="CS200", course_name="OTHER", programme_term=2, credit_hours=3
    )
    other = planner_course_key("CS200", "OTHER")
    ots.budget(sc, other, 1, code="CS200")
    ots.link("AI", "CS200", a)
    ts = ots.section(sc, other, "S1")
    ots.place(board, ts, [("SUN", "09:00", "10:15"), ("TUE", "13:00", "14:15")])
    _pin(sc, other, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, ("SUN",), "09:00", "11:45")
    rule(a, KIND_AVAILABLE_ONLY, ("TUE",), "13:00", "15:45")
    return sc, key, a


LEGAL_FREE_WEEK = [("SUN", "10:30", "11:45"), ("TUE", "14:30", "15:45")]


def test_the_same_index_feed_misses_the_legal_week_the_complete_set_holds(av_on) -> None:
    """The generator-level half of CON-2: the per-teacher feed never builds
    SUN 10:30 + TUE 14:30; the complete set does, after the feed's own options
    (in their order), and every week in it is legal."""
    from core.services.timetable_autoplace import (
        DEFAULT_LAB_SLOTS,
        DEFAULT_SLOTS,
        availability_complete_options,
        availability_section_options,
    )
    from tests.test_instructor_availability_engine import _av

    rules = _av(
        (7, KIND_AVAILABLE_ONLY, ("SUN",), 9 * 60, 11 * 60 + 45),
        (7, KIND_AVAILABLE_ONLY, ("TUE",), 13 * 60, 15 * 60 + 45),
    )
    args = (rules, (7,), "CS101", 3, DEFAULT_SLOTS, DEFAULT_LAB_SLOTS, [])
    kwargs = {"course_key": "CS101", "meeting_pattern": "", "course_options": []}

    def weeks(options):
        return [tuple((m["day"], m["start"], m["end"]) for m in option) for option in options]

    feed = weeks(availability_section_options(*args, **kwargs))
    complete = weeks(availability_complete_options(*args, **kwargs))
    target = tuple(LEGAL_FREE_WEEK)
    assert target not in feed
    assert target in complete
    # The feed's own list (repeats included) first, then only new weeks, once.
    assert complete[: len(feed)] == feed
    extra = complete[len(feed) :]
    assert len(set(extra)) == len(extra) and not set(extra) & set(feed)
    assert set(complete) == {
        (("SUN", s1, e1), ("TUE", s2, e2))
        for s1, e1 in (("09:00", "10:15"), ("10:30", "11:45"))
        for s2, e2 in (("13:00", "14:15"), ("14:30", "15:45"))
    }
    # Control: with no rule the complete set is every SUN/TUE pair of the seven
    # lecture periods (7 x 7), not only the 7 same-period weeks.
    open_week = weeks(
        availability_complete_options(helper.EMPTY_AVAILABILITY, (), *args[2:], **kwargs)
    )
    assert sum(1 for week in open_week if [d for d, _s, _e in week] == ["SUN", "TUE"]) == 49


@pytest.mark.django_db
@pytest.mark.parametrize("pinned", [True, False], ids=["fixed", "engine"])
def test_a_legal_clash_free_week_outside_the_feed_is_found(av_on, pinned) -> None:
    """construction:CON-2 end to end: the section lands at SUN 10:30 + TUE 14:30
    for a pinned teacher (not ranked last) and for an engine teacher (placed,
    nothing left unplaced, charged as before)."""
    sc, key, a = _split_windows(f"d-c {pinned}", pinned=pinned)

    if pinned:
        result = _greedy(sc)
    else:
        with sta.pinned_assignment(sc.id, _engine_map(sc, key, a)):
            result = _greedy(sc)

    assert _placed_meetings(sc, key) == LEGAL_FREE_WEEK
    report = _board_result(result)["instructor_availability"]
    assert report["ranked_last"] == []
    assert report["unplaced"] == []
    assert report.get("released", []) == []
    assert not _blocked(_placed_meetings(sc, key), a, _rules_of(a))


@pytest.mark.django_db
def test_the_one_teacher_assignment_keeps_the_complete_legal_set(av_on) -> None:
    """``_Static.teacher_options`` (the guard's pending options, the exact
    solve's reservations) holds the week the feed misses, so the free-option
    rule sees it as free: the closure charges Dr A with the missing CS101 S1."""
    sc, key, a = _split_windows("d-c static", pinned=False)
    static = sta._load_static(sc.id)
    av = helper.availability_for(sc.id, universe=[a.pk])

    cells = static.teacher_options(key, static.budgets[key], a.pk, av)

    sun, tue = 0, 2
    assert ((sun, 10 * 60 + 30, 11 * 60 + 45), (tue, 14 * 60 + 30, 15 * 60 + 45)) in cells
    assert sta.load_assignment(sc.id).teacher[f"{key}_S1"] == a.pk


@pytest.mark.django_db
def test_a_rule_that_closes_no_cell_of_the_grid_leaves_the_slot_alone(av_on) -> None:
    """The complete set's baseline is complete too: a teacher whose rule closes
    no daytime cell ("unavailable from 19:00") gets no per-teacher entry, so the
    missing section's slot is exactly the one without the rule. Control: a rule
    that closes one cell gives the entry."""
    sc, _board, key, (a,) = _greedy_scenario("d-c baseline")
    plain = {slot.key: slot for slot in sta.collect_slots(sc.id)}
    rule(a, KIND_UNAVAILABLE, start="19:00")

    ruled = {slot.key: slot for slot in sta.collect_slots(sc.id)}
    assert ruled == plain
    assert ruled[f"{key}_S1"].teacher_options == ()

    rule(a, KIND_UNAVAILABLE, ("SUN",), "16:00", "17:15")
    helper_slots = {slot.key: slot for slot in sta.collect_slots(sc.id)}
    ((teacher, legal),) = helper_slots[f"{key}_S1"].teacher_options
    assert teacher == a.pk and legal


# ── D-B: an engine teacher is released, never the section left unplaced ───


@pytest.mark.django_db
@pytest.mark.parametrize("released", [True, False], ids=["released", "no-release-control"])
def test_every_read_of_the_run_sees_a_released_section(av_on, released) -> None:
    """The mechanism (fix plan D-B 3-4). Dr A (no mornings, so he has a legal
    week) is stored as S1's engine teacher while S1 sits on Sunday and Tuesday
    mornings; Dr B may teach it too. Once the run releases S1 from Dr A:

    - the map pinned when it was released reads S1 TBD (reason availability);
      another pinned map does not;
    - an ENGINE read ignores Dr A's stored row, so its closure picks Dr B; a
      display read keeps Dr A (fix plan D-H);
    - a re-pick's kept rule and a closure's seed never keep Dr A;
    - storing a map closure-complete on the layout resolves every release;
    - inside one request's memo, the engine read after the release is not served
      the plain map read before it.

    Control: without the release Dr A is kept everywhere (the kept rule)."""
    sc, _board, key, _ts, (a, b) = _ruled_board()
    _engine_row(sc, key, "S1", a)
    rule(a, KIND_UNAVAILABLE, end="13:00")
    sid = f"{key}_S1"

    # One request's memo too: the engine read after the release must not be
    # served the plain map read before it on the same layout.
    with sta.assignment_scope(sc.id):
        stored = sta.load_assignment(sc.id, incomplete="collect")
        assert stored.teacher[sid] == a.pk  # kept: he has a legal week
        assert sta.load_assignment(sc.id, release_unteachable=True) is stored  # memoised
        other_pin = replace(stored)
        with sta.pinned_assignment(sc.id, stored):
            if released:
                sta.release_in_run(sc.id, sid, (a.pk,))
            pinned = sta.load_assignment(sc.id)
            with sta.pinned_assignment(sc.id, other_pin):
                elsewhere = sta.load_assignment(sc.id)
        engine = sta.load_assignment(sc.id, release_unteachable=True)
        display = sta.load_assignment(sc.id, incomplete="collect")
        proposed = sta.propose_layout_assignment(sc.id, current=stored, keep_violations_of=stored)
        closed = sta.close_on_layout(sc.id, stored)
        assert bool(helper.released_sections(sc.id)) is released
        sta.persist_assignment(sc.id, engine)
        assert helper.released_sections(sc.id) == {}

    expected = b.pk if released else a.pk
    assert pinned.teacher[sid] == (None if released else a.pk)
    if released:
        assert (pinned.status[sid], pinned.tbd_reason[sid]) == (sta.STATUS_TBD, "availability")
    assert elsewhere.teacher[sid] == a.pk
    assert display.teacher[sid] == a.pk
    assert engine.teacher[sid] == expected
    assert proposed.teacher[sid] == expected
    assert closed.teacher[sid] == expected
    assert ots.engine_rows(sc)[(key, "S1")] == ("Dr B" if released else "Dr A")


def _shared_course(name: str):
    """CS101 is a campus course on the AI and the DS board of term 2 (one budgeted
    section, placed on both). Its one teacher Dr A may teach only SUN and TUE
    13:00-14:15, and his pinned CS200 S1 holds SUN 13:00 on the AI board, so no
    week he may teach is free."""
    from core.services.course_identity import planner_course_key

    sc, ai_board, key, (a,) = _greedy_scenario(name, programs=("AI", "DS"))
    DeliveryBoard.objects.create(
        scenario=sc, label="Term 2 DS", nominal_term=2, program="DS", display_order=2
    )
    ProgrammeRequirement.objects.create(
        program="DS", course_code="CS101", course_name="INTRO", programme_term=2, credit_hours=3
    )
    ots.link("DS", "CS101", a)
    ProgrammeRequirement.objects.create(
        program="AI", course_code="CS200", course_name="OTHER", programme_term=2, credit_hours=3
    )
    other = planner_course_key("CS200", "OTHER")
    ots.budget(sc, other, 1, code="CS200")
    ots.link("AI", "CS200", a)
    ts = ots.section(sc, other, "S1")
    ots.place(ai_board, ts, [("SUN", "13:00", "14:15"), ("TUE", "14:30", "15:45")])
    _pin(sc, other, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, ("SUN", "TUE"), "13:00", "14:15")
    return sc, key, a


@pytest.mark.django_db
def test_a_later_board_of_the_run_places_a_released_sections_copy_teacher_less(av_on) -> None:
    """fix plan D-B 3: the AI board releases S1 from Dr A; the DS board, later in
    the same run, reads the run's map with S1 charged to nobody, so it places
    its copy without asking Dr A's rules again (no second release) and without
    his ledger. Every copy is placed."""
    sc, key, a = _shared_course("d-b later board")
    engine = _engine_map(sc, key, a)

    with helper.availability_scope(sc.id), sta.pinned_assignment(sc.id, engine):
        result = _greedy(sc)

    boards = result["boards"]
    released = {
        label: board["instructor_availability"]["released"] for label, board in boards.items()
    }
    assert released == {
        "Term 2": [{"section": f"{key}|S1", "instructor_ids": [a.pk]}],
        "Term 2 DS": [],
    }
    for board in DeliveryBoard.objects.filter(scenario=sc):
        assert len(_placed_meetings(sc, key, board=board)) == 2, board.label


def _shortfall_board(name: str, *, ruled: bool, stored: bool):
    """adversary:ADV-1's place-missing case on one board. CS101 runs four
    sections; Dr A is its only linked teacher and may teach only SUN and TUE
    08:00-12:00. S1 (pinned to Dr B) sits SUN/TUE 10:50 and S2 SUN/TUE 09:00;
    S3 and S4 are missing. The free-option rule gives Dr A S3 (SUN/TUE 10:30 is
    clear of his S2), but the greedy's same-course filter takes that week (it
    overlaps S1), and every other week he may teach clashes with S2. D10 leaves
    S4 to be decided. ``stored``: the map is stored first (an ensure), so Dr A
    holds S3 as a stored engine row."""
    from core.services.course_identity import planner_course_key

    sc, board = ots.scenario(name, programs=("AI",))
    ProgrammeRequirement.objects.create(
        program="AI", course_code="CS101", course_name="INTRO", programme_term=2, credit_hours=3
    )
    key = planner_course_key("CS101", "INTRO")
    ots.budget(sc, key, 4)
    a, b = _person("Dr A"), _person("Dr B")
    ots.link("AI", "CS101", a)
    s1 = ots.section(sc, key, "S1")
    ots.place(board, s1, [("SUN", "10:50", "12:05"), ("TUE", "10:50", "12:05")])
    _pin(sc, key, "S1", b)
    s2 = ots.section(sc, key, "S2")
    ots.place(board, s2, [("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15")])
    if ruled:
        rule(a, KIND_AVAILABLE_ONLY, ("SUN", "TUE"), "08:00", "12:00")
    if stored:
        sta.prepare_section_teachers(sc.id, phase="ensure")
        assert ots.engine_rows(sc).get((key, "S3")) == "Dr A"
    return sc, key, a


def _missing(sc) -> dict:
    from core.services.timetable_required_sections import (
        find_missing_required_sections,
        sections_short_by_course,
    )

    return sections_short_by_course(find_missing_required_sections(sc.id))


@pytest.mark.django_db
@pytest.mark.parametrize("ruled", [True, False], ids=["rule", "no-rule-control"])
@pytest.mark.parametrize("path", ["place-missing", "reinstatement"])
def test_a_missing_section_is_placed_with_its_engine_teacher_released(av_on, path, ruled) -> None:
    """fix plan D-B acceptance: place-missing (``run_placer(rebuild=False)``) and
    the required-section reinstatement both end with S3 and S4 placed, and the
    released S3 is TBD or charged to a teacher who may teach it (Dr A's stored
    row is replaced: the kept rule does not keep a teacher the run released).
    The persisted board breaks no rule under the final map. Control: without
    the rule both sections are placed too, and nothing is released."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_required_sections import reinstate_missing_required_sections

    sc, key, a = _shortfall_board(f"d-b {path} {ruled}", ruled=ruled, stored=True)

    if path == "place-missing":
        result = sta.run_placer(sc.id, lambda: auto_place_scenario(sc.id), rebuild=False)
        released = [
            row
            for board in result["boards"].values()
            for row in board.get("instructor_availability", {}).get("released", [])
        ]
    else:
        report = reinstate_missing_required_sections(sc.id)
        released = report.get("instructor_availability", {}).get("released", [])

    assert _missing(sc) == {}
    final = sta.load_assignment(sc.id, incomplete="collect")
    assert _board_violations(sc)[0] == 0
    if ruled:
        assert released == [{"section": f"{key}|S3", "instructor_ids": [a.pk]}]
        assert final.teacher[f"{key}_S3"] != a.pk
        assert ots.engine_rows(sc).get((key, "S3")) is None
        # Nobody's name, least of all the released teacher's, is written on it.
        assert "Dr A" not in set(
            TermSectionMeeting.objects.filter(
                term_section__scenario=sc, term_section__course_key=key, term_section__section="S3"
            ).values_list("instructor", flat=True)
        )
    else:
        assert released == []
        assert final.teacher[f"{key}_S3"] == a.pk


#: The parity corpus's rule sets of the adversary review (ADV-1): "no
#: afternoons" for Dr Amal, and its random rule set 2.
CORPUS_RULES = {
    "no-rule-control": [],
    "amal-no-afternoons": [
        ("Dr Amal", KIND_UNAVAILABLE, ("SUN", "MON", "TUE", "WED", "THU"), "12:00", "24:00")
    ],
    "random-set-2": [
        ("Dr Amal", KIND_UNAVAILABLE, ("SUN", "MON", "TUE"), "09:00", "19:00"),
        ("Dr Eyad", KIND_UNAVAILABLE, ("THU",), "10:00", "20:00"),
    ],
}
CLASSIC_STRATEGIES = (
    "compact",
    "morning",
    "balanced",
    "load_balanced",
    "optimal",
    "hybrid",
    "adaptive",
)


@pytest.mark.django_db
@pytest.mark.parametrize("rules", list(CORPUS_RULES))
@pytest.mark.parametrize("strategy", CLASSIC_STRATEGIES)
def test_every_classic_strategy_places_every_section_of_the_corpus(av_on, strategy, rules) -> None:
    """fix plan D-B acceptance on the parity corpus: every classic Generate
    strategy through ``run_placer(rebuild=True)`` places every budgeted section,
    and no charged teacher meets in a time his rule forbids under the final
    map. (The solver budgets are capped at one second, which keeps the test
    short and changes nothing the assertions read.)"""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_run_seed import seeded_timetable_run
    from tests.test_one_teacher_flag_parity import build_corpus

    sc = build_corpus(11)
    for name, kind, days, start, end in CORPUS_RULES[rules]:
        rule(Instructor.objects.get(full_name=name), kind, days, start, end)

    with seeded_timetable_run(11):
        sta.run_placer(
            sc.id,
            lambda: auto_place_scenario(sc.id, strategy=strategy, candidate_gen_budget=1.0),
            rebuild=True,
        )

    assert _missing(sc) == {}
    assert _board_violations(sc)[0] == 0


@pytest.mark.django_db
@pytest.mark.parametrize("ruled", [True, False], ids=["rule", "no-rule-control"])
def test_a_released_section_leaves_its_teachers_ledger_and_guard(av_on, monkeypatch, ruled) -> None:
    """fix plan D-B 3: once CS101 S1 is released, the greedy no longer counts its
    meetings against Dr A (his clash and cap ledger) and the one-teacher guard
    no longer keeps one of its options free for him. Read at the next section
    the same board places (a 2-credit CS050). Control: without the rule Dr A
    teaches S1, so both hold it."""
    from core.models import InstructorTimeRule
    from tests.test_instructor_availability_engine import _clash_taken

    sc, key, a = _clash_taken(f"d-b ledger {ruled}", pinned=False)
    if not ruled:
        InstructorTimeRule.objects.filter(instructor=a).delete()
    later = ots.requirement("AI", "CS050", "LATER", credit=2)
    ots.budget(sc, later, 1, code="CS050", credit=2)
    seen: dict = {}
    real = sta.PlacerGuard.check

    def spy(self, key_, teachers, schedule, **kwargs):
        if key_.startswith("CS050"):
            seen["ledger"] = {section for section, *_rest in schedule.get(a.pk, ())}
            seen["pending"] = {other for other, _options in self.pending.get(a.pk, ())}
        return real(self, key_, teachers, schedule, **kwargs)

    monkeypatch.setattr(sta.PlacerGuard, "check", spy)
    with helper.availability_scope(sc.id), sta.pinned_assignment(sc.id, _engine_map(sc, key, a)):
        _greedy(sc)

    assert len(_placed_meetings(sc, key)) == 2
    assert (f"{key}|S1" in seen["ledger"]) is not ruled
    assert (f"{key}|S1" in seen["pending"]) is not ruled


# ── D-M: CP-SAT holds a fixed teacher without a legal week to his fewest ──


CPSAT_BUILDERS = ("solve_board", "solve_board_with_hints")


def _shapes_board(name: str, *, ruled: bool):
    """adversary:ADV-2's shapes board: CS101 (two 2 x 75 sections), AI201 (4
    credits) and ENG101 on one board. Dr A is pinned to CS101 S1 and may teach
    only on Thursday, so every week of CS101 holds at least one meeting he may
    not teach (exactly one at best)."""
    from core.services.course_identity import planner_course_key

    sc, board = ots.scenario(name, programs=("AI",))
    keys = {}
    for code, course, credit, planned, teacher in (
        ("CS101", "INTRO", 3, 2, "Dr A"),
        ("AI201", "ML", 4, 1, "Dr B"),
        ("ENG101", "ENGLISH", 3, 1, "Dr E"),
    ):
        ProgrammeRequirement.objects.create(
            program="AI",
            course_code=code,
            course_name=course,
            programme_term=2,
            credit_hours=credit,
        )
        keys[code] = planner_course_key(code, course)
        ots.budget(sc, keys[code], planned, code=code, credit=credit)
        ots.link("AI", code, _person(teacher))
    a = _person("Dr A")
    _pin(sc, keys["CS101"], "S1", a)
    if ruled:
        rule(a, KIND_AVAILABLE_ONLY, ("THU",))
    return sc, board, keys["CS101"], a


@pytest.mark.django_db
@pytest.mark.parametrize("ruled", [True, False], ids=["rule", "no-rule-control"])
def test_optimal_keeps_its_cpsat_build_with_a_fixed_teacher_without_a_legal_week(
    av_on, caplog, ruled
) -> None:
    """adversary:ADV-2 / fix plan D-M: 'optimal' (through ``run_placer``) keeps its
    CP-SAT board; the pinned section holds exactly one forbidden meeting (the
    fewest any week has) and the board reports it. It used to be solved
    unpruned, so the persist guard threw the whole board away and 'optimal'
    fell back to compact. Control: no rule, no fallback, no report key."""
    import logging

    from core.services.timetable_autoplace import auto_place_scenario

    sc, _board, key, a = _shapes_board(f"d-m {ruled}", ruled=ruled)

    with caplog.at_level(logging.WARNING, logger="core.services.timetable_autoplace"):
        result = sta.run_placer(
            sc.id, lambda: auto_place_scenario(sc.id, strategy="optimal"), rebuild=True
        )

    assert not [r for r in caplog.records if "falling back to compact" in r.getMessage()]
    board = _board_result(result)
    assert board["status"] in ("optimal", "feasible") and not board.get("persist_rolled_back")
    assert _missing(sc) == {}
    meetings = _placed_meetings(sc, key)
    assert len(meetings) == 2
    if ruled:
        assert len(_blocked(meetings, a, _rules_of(a))) == 1, meetings
        assert board["instructor_availability"]["ranked_last"] == [
            {"section": f"{key}|S1", "instructor_ids": [a.pk], "reason": "rule_cannot_be_met"}
        ]
    else:
        assert "instructor_availability" not in board


@pytest.mark.django_db
@pytest.mark.parametrize("builder", CPSAT_BUILDERS)
def test_both_builders_hold_a_fixed_teacher_to_his_fewest_forbidden_meetings(
    av_on, builder
) -> None:
    """fix plan D-M in each builder (T-C5 strengthened): the pinned "only
    Thursday" section is placed with exactly one meeting off Thursday, the
    minimum (unconstrained, the model is free to put both elsewhere)."""
    from tests.test_instructor_availability_engine import _solve

    sc, board, key, (a,) = _greedy_scenario(f"d-m {builder}", pin=True)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    result = _solve(builder, board)

    assert result["status"] in ("optimal", "feasible") and result["placed"] == 1
    meetings = [(m["day"], m["start"], m["end"]) for m in result["placements"][0]["meetings"]]
    assert len(_blocked(meetings, a, _rules_of(a))) == 1, meetings
    assert result["instructor_availability"]["ranked_last"] == [
        {"section": f"{key}|S1", "instructor_ids": [a.pk], "reason": "rule_cannot_be_met"}
    ]


def _sunday_and_monday_mornings(person) -> None:
    """No ENG bundle is legal: every bundle meets on Sunday. The AM bundle also
    meets on Monday morning, so it breaks the rule twice, the PM bundle once."""
    rule(person, KIND_UNAVAILABLE, ("SUN",))
    rule(person, KIND_UNAVAILABLE, ("MON",), "09:00", "12:00")


@pytest.mark.django_db
@pytest.mark.parametrize("builder", CPSAT_BUILDERS)
@pytest.mark.parametrize("pinned", [True, False], ids=["fixed", "engine"])
def test_an_english_section_without_a_legal_bundle_in_cpsat(av_on, builder, pinned) -> None:
    """identity:F4 (G13) and construction:CON-4 (mutant D). A FIXED teacher's
    English section without a legal bundle keeps the bundle with the fewest
    forbidden meetings (PM) and the board solves; an ENGINE teacher's is left
    no bundle, so the board is infeasible and its caller keeps the greedy."""
    from tests.test_instructor_availability_engine import _solve

    sc, board, key, (a,) = _greedy_scenario(
        f"d-m eng {builder} {pinned}", code="ENG101", pin=pinned
    )
    _sunday_and_monday_mornings(a)

    if pinned:
        result = _solve(builder, board)
    else:
        with sta.pinned_assignment(sc.id, _engine_map(sc, key, a)):
            result = _solve(builder, board)

    if pinned:
        assert result["status"] in ("optimal", "feasible") and result["placed"] == 1
        starts = {m["start"] for m in result["placements"][0]["meetings"]}
        assert starts == {"13:00"}, starts
    else:
        assert result["status"] == "infeasible" and result["placed"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize("builder", CPSAT_BUILDERS)
@pytest.mark.parametrize("days", [("SUN", "TUE"), ("SUN",)], ids=["two-days", "one-day"])
def test_a_fixed_online_window_section_is_pruned_at_exactly_enough_days(
    av_on, monkeypatch, builder, days
) -> None:
    """construction:CON-7 (mutant H): GS101 is online on the AI board but campus
    elsewhere, so its pinned teacher's rules bind. He may teach the first
    evening window on exactly as many days as it meets (two): that is a legal
    week, and its domains are pruned to those two cells. Control: one such day
    is not a legal week; the domains stay whole (the fewest-forbidden cap holds
    it to one forbidden meeting instead)."""
    from core.models import InstructorTimeRule
    from tests.test_instructor_availability_engine import _domain_sizes, _online_here

    av_on.TIMETABLE_ONLINE_EVENING_WINDOWS = True
    sc, ai_board, key, a = _online_here(elsewhere=True)
    InstructorTimeRule.objects.filter(instructor=a).delete()
    _pin(sc, key, "S1", a)
    rule(a, KIND_AVAILABLE_ONLY, days, "15:50", "17:30")

    sizes = _domain_sizes(monkeypatch, builder, ai_board)

    assert sizes == ([2, 2] if len(days) == 2 else [15, 15])


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("pinned", "rule_kind"),
    [(True, "only-thursday"), (True, "no-mornings"), (False, "only-thursday")],
    ids=["fixed-without-legal-week", "fixed-with-legal-week", "engine"],
)
def test_the_persist_guard_forgives_only_a_fixed_teachers_unavoidable_meetings(
    av_on, pinned, rule_kind
) -> None:
    """fix plan D-M: the persist guard forgives a section exactly the forbidden
    meetings it cannot avoid, and only when its teacher is FIXED and has no
    legal week; a fixed teacher with a legal week and an engine teacher are
    forgiven nothing."""
    sc, _board, key, (a,) = _greedy_scenario(f"d-m guard {rule_kind} {pinned}", pin=pinned)
    if rule_kind == "only-thursday":
        rule(a, KIND_AVAILABLE_ONLY, ("THU",))
    else:
        rule(a, KIND_UNAVAILABLE, end="13:00")
    av = helper.availability_for(sc.id)

    if pinned:
        forgiven = sta.unavoidable_violations(sc.id, av)
    else:
        with sta.pinned_assignment(sc.id, _engine_map(sc, key, a)):
            forgiven = sta.unavoidable_violations(sc.id, av)

    expected = {f"{key}_S1": 1} if (pinned and rule_kind == "only-thursday") else {}
    assert forgiven == expected


# ── D-N: hybrid and load_balanced keep the greedy boards' report ──────────


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
@pytest.mark.parametrize("strategy", ["hybrid", "load_balanced", "compact"])
def test_every_strategy_reports_a_fixed_teacher_placed_against_his_rule(
    av_on, strategy, flag
) -> None:
    """adversary:ADV-3 / fix plan D-N: hybrid and load_balanced rebuild their
    result from the database; they now carry the greedy boards' report, so the
    pinned "only Thursday" section placed against its rule is listed by them as
    by compact. Control: with the flag off no strategy has the key."""
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _board, key, (a,) = _greedy_scenario(f"d-n {strategy} {flag}", pin=True)
    rule(a, KIND_AVAILABLE_ONLY, ("THU",))

    board = _board_result(_greedy(sc, strategy, candidate_gen_budget=0.5))

    if flag:
        assert board["instructor_availability"]["ranked_last"] == [
            {"section": f"{key}|S1", "instructor_ids": [a.pk], "reason": "rule_cannot_be_met"}
        ]
    else:
        assert "instructor_availability" not in board


# ── D-O: with the flag OFF no rule entry point runs and no key appears ─────


def _tripwires(monkeypatch) -> None:
    """Every door to the stored rules and the placed-online scan raises."""
    from core.services import instructor_availability as rules_module

    def tripped(*_args, **_kwargs):
        raise AssertionError("a teaching-time rule entry point ran with the flag off")

    for module in (helper, rules_module):
        for name in ("load_time_rules", "load_instructor_availability", "compile_rules"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tripped)
    monkeypatch.setattr(helper, "_availability_of_the_scenario", tripped)
    monkeypatch.setattr(helper.OnlineExemption, "placed_rows", tripped)


def _flag_off_corpus(settings):
    """The parity corpus placed by compact with the rules flag OFF (one teacher on)
    and a rule stored for Dr Amal, so loading it would make a difference."""
    from core.services.timetable_autoplace import auto_place_scenario
    from tests.test_one_teacher_flag_parity import build_corpus

    for name, value in AVAILABILITY_FLAGS.items():
        setattr(settings, name, value)
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    sc = build_corpus(11)
    rule(Instructor.objects.get(full_name="Dr Amal"), KIND_UNAVAILABLE, start="12:00")
    first = auto_place_scenario(sc.id, strategy="compact")
    return sc, first


FLAG_OFF_PATHS = (
    "greedy",
    "reinstatement",
    "annealing",
    "balancer",
    "solver",
    "persist_guard",
    "optimise_current",
    "repair_analysis",
)


@pytest.mark.django_db
@pytest.mark.parametrize("path", FLAG_OFF_PATHS)
def test_with_the_flag_off_no_rule_entry_point_runs_and_no_key_appears(
    settings, monkeypatch, path
) -> None:
    """identity:F2 / fix plan D-O, pinned absolutely (not relative to flag on):
    with the flag OFF every rule loader and the placed-online scan are
    tripwires, and each automatic path completes without touching them; the
    greedy board result, the reinstatement report, the persist guard's capture
    and the evaluator's quality score carry no availability key."""
    from core.models import ScenarioStudentCourseRequest, SectionPlacement, TermSectionMeeting
    from core.services.timetable_autoplace import auto_place_scenario

    sc, first = _flag_off_corpus(settings)
    _tripwires(monkeypatch)
    board = DeliveryBoard.objects.filter(scenario=sc).order_by("display_order").first()

    if path == "greedy":
        result = auto_place_scenario(sc.id, strategy="compact")
        for one in (first, result):
            assert all("instructor_availability" not in b for b in one["boards"].values())
    elif path == "reinstatement":
        from core.services.timetable_required_sections import reinstate_missing_required_sections

        victim = SectionPlacement.objects.filter(board=board).first().term_section
        SectionPlacement.objects.filter(term_section=victim).delete()
        TermSectionMeeting.objects.filter(term_section=victim).delete()
        report = reinstate_missing_required_sections(sc.id)
        assert report["attempted"] >= 1 and report["after_missing"] == []
        assert "instructor_availability" not in report
    elif path == "annealing":
        from core.services.timetable_local_search import optimize_and_persist_board

        optimize_and_persist_board(board.id, max_seconds=0.3)
    elif path == "balancer":
        from core.services.timetable_load_balanced import rebalance_and_persist_board

        rebalance_and_persist_board(board.id, max_seconds=0.3)
    elif path == "solver":
        from core.services.timetable_solver import solve_and_persist_board

        result = solve_and_persist_board(board.id, time_limit_seconds=1.0)
        assert "instructor_availability" not in result
    elif path == "persist_guard":
        from core.services.timetable_local_search import _capture_persist_guard

        assert "availability" not in _capture_persist_guard(board)
    elif path == "optimise_current":
        from core.services.timetable_v2_runner import run_v2_optimisation_guarded

        result = run_v2_optimisation_guarded(
            sc.id, mode="current", max_iterations=2, run_chain=False, run_cpsat=False
        )
        assert "error" not in result, result.get("error")
        assert "instructor_availability" not in (result.get("quality_score") or {})
    else:
        from core.services.timetable_repair import analyse_timetable_repair

        placement = SectionPlacement.objects.filter(board=board).order_by("id").first()
        student = (
            ScenarioStudentCourseRequest.objects.filter(
                scenario=sc, course_key=placement.term_section.course_key
            )
            .values_list("student_id", flat=True)
            .first()
        )
        created = analyse_timetable_repair(
            placement_id=placement.id, blocked_student_ids=[student], limits={"max_candidates": 2}
        )
        assert created["run"]["id"]
