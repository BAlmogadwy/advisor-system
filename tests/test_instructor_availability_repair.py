"""``TIMETABLE_INSTRUCTOR_AVAILABILITY``, slice B2: the repair pass
(``repair_instructor_availability``, decisions B2-1 and B2-2 of the brief).

The pass moves a meeting that breaks its charged teacher's teaching-time rule to
a cell the teacher may teach. It never unplaces, never moves a frozen meeting,
never makes a move the instructor-week gate blocks, and a rule no week can meet
is reported. These tests call it on its own (nothing wires it into the optimiser
yet). Every behavioural test has a control: the pass skipped (an empty
availability), or no rule binding a teacher of the scenario.
"""

from __future__ import annotations

import pytest

from core.models import ScenarioSectionBudget, SectionPlacement, TermSection, TermSectionMeeting
from core.services import timetable_instructor_cap_repair as cap_repair
from core.services.instructor_availability import EMPTY_AVAILABILITY, KIND_UNAVAILABLE
from core.services.timetable_optimisation_policy import week_move_breaks_gate
from tests._instructor_availability_fixture import ALL_DAYS
from tests.test_instructor_availability_engine import _av, _dr, _inject, _unavailable
from tests.test_section_day_run import _SLOTS, _instructor_board

REPAIR_FLAGS = {
    "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP": 3,
    "TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED": False,
    "TIMETABLE_SECTION_DAY_SPREAD": False,
}

AM = ("09:00", "10:15")
LATE_AM = ("10:30", "11:45")
PM = ("13:00", "14:15")


@pytest.fixture
def repair_flags(settings):
    for name, value in REPAIR_FLAGS.items():
        setattr(settings, name, value)
    return settings


def _repair(scenario_id: int) -> dict:
    return cap_repair.repair_instructor_availability(scenario_id)


def _cells(scenario, course: str) -> list[tuple[str, str]]:
    order = {day: i for i, day in enumerate(ALL_DAYS)}
    rows = SectionPlacement.objects.filter(
        board__scenario=scenario, term_section__course_code=course
    ).values_list("day", "start_time")
    return sorted(rows, key=lambda row: (order[row[0]], row[1]))


def _no_mornings(person: int):
    return _av((person, KIND_UNAVAILABLE, ALL_DAYS, 0, 13 * 60))


def _rule_or_not(monkeypatch, mode: str, rule) -> None:
    """``rule``: the teacher's rule. ``empty``: the pass is skipped (control).
    ``other``: a rule only for a teacher the scenario never charges (control)."""
    if mode == "rule":
        _inject(monkeypatch, rule)
    elif mode == "empty":
        _inject(monkeypatch, EMPTY_AVAILABILITY)
    else:
        _inject(monkeypatch, _unavailable(987654, *ALL_DAYS))


MODES = pytest.mark.parametrize("mode", ["rule", "empty", "other-teacher"])


@pytest.mark.django_db
def test_flag_off_the_pass_is_a_no_op_with_no_query(django_assert_num_queries) -> None:
    """The flag is off by default: the report says disabled and nothing is read."""
    with django_assert_num_queries(0):
        report = _repair(1)
    assert report["enabled"] is False and report["repaired"] == [] and report["detected"] == []


# ── T-B1 / T-B2: where a violating meeting goes ──────────────────────────


@pytest.mark.django_db(transaction=True)
@MODES
def test_a_violating_meeting_moves_to_a_legal_cell_on_a_day_the_teacher_works(
    repair_flags, monkeypatch, mode
) -> None:
    """T-B1: Dr Run may not teach on WED, but C1 meets SUN and WED. TUE, MON and THU
    are legal; he already works TUE (C2), so C1 goes to TUE, the first free time (10:30:
    09:00 is C2's), though MON comes first in the week. Controls: the pass skipped (empty availability) and a rule on a
    teacher the scenario never charges leave the board as it was."""
    scenario = _instructor_board(
        f"t-b1 {mode}",
        [("C2", "S1", [("TUE", *AM)]), ("C1", "S1", [("SUN", *AM), ("WED", *AM)])],
    )
    _rule_or_not(monkeypatch, mode, _unavailable(_dr(), "WED"))

    report = _repair(scenario.id)

    if mode != "rule":
        assert _cells(scenario, "C1") == [("SUN", "09:00"), ("WED", "09:00")]
        assert report["repaired"] == [] and report["locked_blocked"] == []
        assert report["enabled"] is (mode == "other-teacher")
        return
    assert _cells(scenario, "C1") == [("SUN", "09:00"), ("TUE", "10:30")]
    assert sorted(
        TermSectionMeeting.objects.filter(
            term_section__scenario=scenario, term_section__course_code="C1"
        ).values_list("day", "start_time")
    ) == [("SUN", "09:00"), ("TUE", "10:30")]
    assert [row["to"] for row in report["repaired"]] == ["TUE 10:30-11:45"]
    assert report["repaired"][0]["from"] == "WED 09:00-10:15"
    assert report["moved_instructors"] == [_dr()]
    assert report["remaining_violations"] == 0 and report["unplaced"] == []
    assert report["instructor_before"] is not None and report["instructor_after"] is not None
    assert len(report["detected"]) == 1


@pytest.mark.django_db(transaction=True)
@MODES
def test_the_same_day_is_offered_as_a_destination(repair_flags, monkeypatch, mode) -> None:
    """T-B2: Dr Run may not teach mornings and C1 meets SUN 09:00 alone. SUN 13:00
    is on a day he already works, so the meeting stays on SUN and moves to the
    afternoon (control: pass skipped / no bound rule: it stays at 09:00)."""
    scenario = _instructor_board(f"t-b2 {mode}", [("C1", "S1", [("SUN", *AM)])])
    _rule_or_not(monkeypatch, mode, _no_mornings(_dr()))

    _repair(scenario.id)

    assert _cells(scenario, "C1") == ([("SUN", "13:00")] if mode == "rule" else [("SUN", "09:00")])


# ── T-B3: frozen meetings are reported, not moved ────────────────────────


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("frozen", ["locked", "external", "none"])
def test_a_frozen_meeting_is_reported_and_not_moved(repair_flags, monkeypatch, frozen) -> None:
    """T-B3: a locked or externally owned section stays where it is and is listed
    under ``locked_blocked`` with its reason. Control (``none``): the same meeting
    unfrozen is moved."""
    scenario = _instructor_board(
        f"t-b3 {frozen}", [("C1", "S1", [("WED", *AM)], frozen == "locked")]
    )
    _inject(monkeypatch, _unavailable(_dr(), "WED"))
    if frozen == "external":
        section_id = TermSection.objects.get(scenario=scenario, course_code="C1").id
        monkeypatch.setattr(cap_repair, "external_shared_section_ids", lambda _sid: {section_id})

    report = _repair(scenario.id)

    if frozen == "none":
        assert _cells(scenario, "C1") != [("WED", "09:00")]
        assert report["locked_blocked"] == [] and len(report["repaired"]) == 1
        return
    assert _cells(scenario, "C1") == [("WED", "09:00")]
    assert [row["reason"] for row in report["locked_blocked"]] == [
        "locked" if frozen == "locked" else "externally_owned"
    ]
    assert report["repaired"] == [] and report["remaining_violations"] == 1


# ── T-B4: a rule no week can meet ────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("closed", [True, False], ids=["no-legal-week", "legal-week-control"])
def test_a_rule_no_week_can_meet_is_unavoidable_and_the_meeting_stays(
    repair_flags, monkeypatch, closed
) -> None:
    """T-B4: with every day closed to Dr Run, no week of C1 is legal: it is
    ``unavoidable`` and does not move. Control: closing only WED leaves legal weeks
    and the meeting moves."""
    scenario = _instructor_board(f"t-b4 {closed}", [("C1", "S1", [("WED", *AM)])])
    ScenarioSectionBudget.objects.create(
        scenario=scenario, course_key="C1", course_code="C1", credit_hours=3, planned_sections=1
    )
    _inject(monkeypatch, _unavailable(_dr(), *(ALL_DAYS if closed else ("WED",))))

    report = _repair(scenario.id)

    if closed:
        assert [row["reason"] for row in report["unavoidable"]] == ["rule_cannot_be_met"]
        assert report["locked_blocked"] == [] and report["repaired"] == []
        assert report["remaining_violations"] == 1
        assert _cells(scenario, "C1") == [("WED", "09:00")]
    else:
        assert report["unavoidable"] == [] and len(report["repaired"]) == 1
        assert _cells(scenario, "C1") != [("WED", "09:00")]


# ── T-B5: never unplaces ─────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("closed", [True, False], ids=["no-cell", "cell-control"])
def test_the_pass_never_unplaces_a_meeting_it_cannot_move(
    repair_flags, monkeypatch, closed
) -> None:
    """T-B5: every cell is closed to Dr Run (no budget, so the week is not judged
    unavoidable): the meeting has no destination, stays placed and is reported
    under ``locked_blocked``; no placement row goes. Control: one open day moves it."""
    scenario = _instructor_board(f"t-b5 {closed}", [("C1", "S1", [("WED", *AM)])])
    before = SectionPlacement.objects.filter(board__scenario=scenario).count()
    _inject(monkeypatch, _unavailable(_dr(), *(ALL_DAYS if closed else ("WED",))))

    report = _repair(scenario.id)

    assert SectionPlacement.objects.filter(board__scenario=scenario).count() == before
    assert report["unplaced"] == []
    if closed:
        assert [row["reason"] for row in report["locked_blocked"]] == [
            "no_legal_slot_preserving_duration"
        ]
        assert _cells(scenario, "C1") == [("WED", "09:00")]
    else:
        assert report["locked_blocked"] == [] and len(report["repaired"]) == 1


# ── T-B6: an English bundle switches whole ───────────────────────────────

ENG_DAYS = ("SUN", "MON", "WED", "THU")


@pytest.mark.django_db(transaction=True)
@MODES
def test_an_english_bundle_switches_from_morning_to_afternoon(
    repair_flags, monkeypatch, mode
) -> None:
    """T-B6: ENG101's morning bundle breaks a "no mornings" teacher's rule four
    times; the afternoon bundle breaks none, so the whole bundle switches. Controls:
    the pass skipped, and a rule for a teacher the scenario never charges."""
    scenario = _instructor_board(
        f"t-b6 {mode}", [("ENG101", "S1", [(day, "09:00", "12:00") for day in ENG_DAYS])]
    )
    _rule_or_not(monkeypatch, mode, _no_mornings(_dr()))

    report = _repair(scenario.id)

    starts = set(
        SectionPlacement.objects.filter(
            board__scenario=scenario, term_section__course_code="ENG101"
        ).values_list("start_time", flat=True)
    )
    if mode == "rule":
        assert starts == {"13:00"}
        assert report["weekly_pattern_search"]["accepted"] == 1
        assert report["detected"] == [] and report["remaining_violations"] == 0
    else:
        assert starts == {"09:00"}
        assert report.get("weekly_pattern_search", {}).get("accepted", 0) == 0


# ── B2-2: the repair never makes a move the week gate blocks ─────────────


def _last_day_off_board(name: str, *, spare_day: bool):
    """Dr Run teaches C5 (WED 09:00, legal), C1 (WED 13:00, forbidden: he may not
    teach on WED after noon), and C2/C3/C4 on SUN/MON/TUE. Every SUN, MON, TUE slot
    and WED 10:30 is a blocked slot, so C1's only legal cells are on THU: a day he
    does not work. With C4 on TUE he works four days (SUN, MON, TUE, WED) and THU
    would be his fifth; without it (``spare_day``) he works three and THU is his
    fourth."""
    closed = [(day, slot) for day in ("SUN", "MON", "TUE") for slot in _SLOTS]
    closed.append(("WED", _SLOTS[1]))
    sessions = [
        ("C2", "S1", [("SUN", *AM)]),
        ("C3", "S1", [("MON", *AM)]),
        ("C5", "S1", [("WED", *AM)]),
        ("C1", "S1", [("WED", *PM)]),
    ]
    if not spare_day:
        sessions.insert(2, ("C4", "S1", [("TUE", *AM)]))
    return _instructor_board(name, sessions, blocked=closed)


def _wed_after_noon(person: int):
    return _av((person, KIND_UNAVAILABLE, ("WED",), 12 * 60, 24 * 60))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("spare_day", [False, True], ids=["last-day-off", "spare-day-control"])
def test_the_only_legal_cell_costing_the_last_day_off_is_not_taken(
    repair_flags, monkeypatch, spare_day
) -> None:
    """B2-2: the only legal cells for C1 are on THU. If that gives Dr Run a fifth
    working day the meeting stays and is reported under ``locked_blocked`` with
    ``needs_extra_working_day``. Control: with a spare day he spends it and C1
    moves to THU."""
    scenario = _last_day_off_board(f"b2-2 {spare_day}", spare_day=spare_day)
    _inject(monkeypatch, _wed_after_noon(_dr()))

    report = _repair(scenario.id)

    if spare_day:
        assert _cells(scenario, "C1") == [("THU", "09:00")]
        assert report["locked_blocked"] == [] and len(report["repaired"]) == 1
    else:
        assert _cells(scenario, "C1") == [("WED", "13:00")]
        assert [row["reason"] for row in report["locked_blocked"]] == ["needs_extra_working_day"]
        assert report["repaired"] == [] and report["remaining_violations"] == 1


def test_the_week_gate_blocks_a_fifth_day_and_allows_a_spare_day() -> None:
    """The gate the repair asks (``week_move_breaks_gate``) outside a day-off scope:
    a teacher newly on five days is blocked; a fourth day is a spare day."""

    def week(days: int) -> dict:
        return {"working_days_by_instructor": {"7": days}}

    assert week_move_breaks_gate(week(4), (), week(5), ()) is True
    assert week_move_breaks_gate(week(3), (), week(4), ()) is False
    assert week_move_breaks_gate(week(5), (), week(5), ()) is False
