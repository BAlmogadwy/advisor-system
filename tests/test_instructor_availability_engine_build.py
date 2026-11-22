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

import pytest

from core.models import ProgrammeRequirement
from core.services import section_teacher_assignment as sta
from core.services import timetable_instructor_availability as helper
from core.services.instructor_availability import KIND_AVAILABLE_ONLY
from tests import one_teacher_support as ots
from tests._instructor_availability_fixture import AVAILABILITY_FLAGS, rule
from tests.test_instructor_availability_engine import (
    _blocked,
    _board_result,
    _engine_map,
    _greedy,
    _greedy_scenario,
    _pin,
    _placed_meetings,
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
    from core.services.instructor_availability import KIND_UNAVAILABLE

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
