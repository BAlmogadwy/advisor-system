"""``TIMETABLE_INSTRUCTOR_AVAILABILITY``, slice B2: the repair pass
(``repair_instructor_availability``, decisions B2-1 and B2-2 of the brief).

The pass moves a meeting that breaks its charged teacher's teaching-time rule to
a cell the teacher may teach. It never unplaces, never moves a frozen meeting,
never makes a move the instructor-week gate blocks, and a rule no week can meet
is reported. The first half of this file calls it on its own; the second half
(below the "second half" banner) covers its wiring into both optimiser
pipelines, the runner's gates and the persisted-board report. Every
behavioural test has a
control: the pass skipped (an empty availability, or the flag off), or no rule
binding a teacher of the scenario.
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
    ``needs_last_day_off``. Control: with a spare day he spends it and C1
    moves to THU."""
    scenario = _last_day_off_board(f"b2-2 {spare_day}", spare_day=spare_day)
    _inject(monkeypatch, _wed_after_noon(_dr()))

    report = _repair(scenario.id)

    if spare_day:
        assert _cells(scenario, "C1") == [("THU", "09:00")]
        assert report["locked_blocked"] == [] and len(report["repaired"]) == 1
    else:
        assert _cells(scenario, "C1") == [("WED", "13:00")]
        assert [row["reason"] for row in report["locked_blocked"]] == ["needs_last_day_off"]
        assert report["repaired"] == [] and report["remaining_violations"] == 1


def test_the_week_gate_blocks_a_fifth_day_and_allows_a_spare_day() -> None:
    """The gate the repair asks (``week_move_breaks_gate``) outside a day-off scope:
    a teacher newly on five days is blocked; a fourth day is a spare day."""

    def week(days: int) -> dict:
        return {"working_days_by_instructor": {"7": days}}

    assert week_move_breaks_gate(week(4), (), week(5), ()) is True
    assert week_move_breaks_gate(week(3), (), week(4), ()) is False
    assert week_move_breaks_gate(week(5), (), week(5), ()) is False


# ══ Slice B2, second half: wiring, gates and the final report ═══════════════
#
# The pass above is wired into both optimiser pipelines (decision B2-3), the runner
# judges it (B2-2, B2-4, B2-5, B2-6) and attaches the persisted-board report (B2-7).
# Every guard has a flag-off or no-rule control.

import dataclasses  # noqa: E402

from core.models import Instructor, SectionInstructor  # noqa: E402
from core.services import section_teacher_assignment as sta  # noqa: E402
from core.services.timetable_instructor_availability_report import (  # noqa: E402
    excused_pairs,
    pair_counts,
    scenario_instructor_availability_report,
)
from tests import one_teacher_support as ots  # noqa: E402
from tests._instructor_availability_fixture import AVAILABILITY_FLAGS, rule  # noqa: E402
from tests.test_one_teacher_day_off_policy import _enrol  # noqa: E402
from tests.test_one_teacher_flag_parity import LINKS  # noqa: E402

RUN = {"max_iterations": 3, "run_chain": False, "run_cpsat": False, "max_chain_iterations": 2}


@pytest.fixture
def av_on(settings):
    """Production's engine flags with the rules ON."""
    for name, value in AVAILABILITY_FLAGS.items():
        setattr(settings, name, value)
    return settings


@pytest.fixture
def run_flags(settings):
    """Production's engine flags with the rules ON and the day-off policy."""
    for name, value in {**AVAILABILITY_FLAGS, **ots.FLAG_ON}.items():
        setattr(settings, name, value)
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = False
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 5
    return settings


def _run(scenario_id: int, **kwargs) -> dict:
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    return run_v2_optimisation_guarded(scenario_id, mode="current", **{**RUN, **kwargs})


def _x_cells(sc, key: str) -> list[tuple[str, str]]:
    rows = SectionPlacement.objects.filter(board__scenario=sc, term_section__course_key=key)
    return sorted(rows.values_list("day", "start_time"))


def _one_section_board(name: str, *, day: str, rule_day: str | None = "WED"):
    """Dr A (the only linked teacher) teaches X (AI301 S1, stored engine row) on
    ``day`` 09:00; he may not teach on ``rule_day``. Students want X (the run needs
    profiles). Returns ``(scenario, course key, Dr A)``."""
    sc, board = ots.scenario(name)
    a = ots.instructor("Dr A")
    x = ots.requirement("AI", "AI301", "X COURSE")
    ots.link("AI", "AI301", a)
    ots.budget(sc, x, 1)
    ots.place(
        board, ots.section(sc, x, "S1"), [(day, "09:00", "10:15")], instructor_name=a.full_name
    )
    SectionInstructor.objects.create(
        scenario=sc, course_key=x, section="S1", instructor=a, source="engine"
    )
    _enrol(sc, x)
    if rule_day is not None:
        rule(a, KIND_UNAVAILABLE, (rule_day,))
    return sc, x, a


# ── B2-3: the pass is wired into Optimise Current and Generate ───────────


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_optimise_current_runs_the_repair_and_rescores_the_board(run_flags, flag) -> None:
    """B2-3 and T-B12: a meeting that breaks its teacher's rule is moved to a legal
    cell by the run itself, the board is RE-SCORED because the pass moved something
    (``_changed_keys``: ``score_before_instructor_passes`` is the pre-pass score the
    student gate judges), and the persisted-board report counts 0. Control: with the
    flag off the meeting stays and no availability key exists."""
    run_flags.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, x, _a = _one_section_board(f"b2-3 {flag}", day="WED")

    result = _run(sc.id)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    if not flag:
        assert _x_cells(sc, x) == [("WED", "09:00")]
        assert "instructor_availability_repair" not in result
        assert "instructor_availability_report" not in result
        assert "score_before_instructor_passes" not in result
        assert "instructor_schedule_after_hard_repairs" not in result
        return
    assert [row["from"][:3] for row in result["instructor_availability_repair"]["repaired"]] == [
        "WED"
    ]
    assert _x_cells(sc, x)[0][0] != "WED"
    assert result["instructor_availability_report"]["count"] == 0
    assert "score_before_instructor_passes" in result
    # H3: the pass's day cost is captured like the cap and clash repairs', before
    # compaction, so the instructor-week gate reads it as a justified repair.
    assert "instructor_schedule_after_hard_repairs" in result


@pytest.mark.django_db
def test_a_run_with_nothing_to_repair_does_not_rescore(run_flags) -> None:
    """T-B12 control: the pass ran (a rule binds Dr A) and moved nothing, so the
    board is not re-scored (no evaluation is bought on every run)."""
    sc, x, _a = _one_section_board("b2-3 clean", day="SUN")

    result = _run(sc.id)

    assert result["instructor_availability_repair"]["repaired"] == []
    assert "score_before_instructor_passes" not in result
    assert _x_cells(sc, x) == [("SUN", "09:00")]


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_the_safety_snapshot_is_taken_when_only_a_rule_binds(run_flags, flag) -> None:
    """B2-3: with the cap, clash and compaction passes all off, a bound rule alone
    makes the optimiser snapshot the safety state BEFORE the instructor passes, so
    the safety gate judges the optimiser's board and not the repair's. Control: the
    flag off takes no snapshot (nothing runs after the search)."""
    run_flags.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    run_flags.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = False
    run_flags.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = False
    run_flags.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = False
    sc, _x, _a = _one_section_board(f"b2-3 snapshot {flag}", day="WED")

    result = _run(sc.id)

    assert "error" not in result, result.get("error")
    assert ("safety_before_instructor_passes" in result) is flag


def test_the_repair_registry_entries_are_present() -> None:
    """B2-3 / H3: the pass is registered with the cap and clash repairs in the
    re-score keys and in the capture of the hard repairs' instructor schedule."""
    import inspect

    from core.services import timetable_optimizer_v2 as v2

    assert '("instructor_availability_repair", ("repaired",))' in inspect.getsource(
        v2.refresh_final_score_after_instructor_passes
    )
    assert '"instructor_availability_repair"' in inspect.getsource(
        v2.capture_instructor_schedule_after_hard_repairs
    )


def test_refresh_rescores_only_when_the_availability_repair_moved_something(monkeypatch) -> None:
    """T-B12 (unit): ``refresh_final_score_after_instructor_passes`` re-scores for a
    moved availability repair and not for an empty one."""
    from types import SimpleNamespace

    from core.services import timetable_optimizer_v2 as v2

    evaluated = []

    def evaluate(**kwargs):
        evaluated.append(kwargs["candidate_id"])
        return SimpleNamespace(
            lexicographic_score=(1,),
            unresolved_student_ids=[],
            quality_score={},
            hotspot_courses=[],
            capacity_pressure_courses=[],
            reserve_heavy_sections=[],
        )

    monkeypatch.setattr(v2, "build_section_states_for_scenario", lambda _sid: [object()])
    monkeypatch.setattr(v2, "evaluate_generated_timetable_candidate", evaluate)
    monkeypatch.setattr(v2, "hotspot_result_fields", lambda _post: {})

    for repaired, rescored in (([{"section": "X"}], True), ([], False)):
        evaluated.clear()
        result = {"final_score": [9], "instructor_availability_repair": {"repaired": repaired}}
        v2.refresh_final_score_after_instructor_passes(result, 1, {"s": 1}, {}, None, None)
        assert bool(evaluated) is rescored
        assert ("score_before_instructor_passes" in result) is rescored


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_generate_runs_the_repair_after_the_clash_repair(run_flags, monkeypatch, flag) -> None:
    """B2-3 (full mode, the other insertion): the pass is called once, after the
    clash repair, only while a rule binds; the flag off never calls it and adds no key."""
    from core.services import timetable_instructor_cap_repair as cap
    from tests.test_one_teacher_flag_parity import build_corpus

    run_flags.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    calls: list[str] = []
    real_clash = cap.repair_instructor_clashes

    def clash(scenario_id):
        calls.append("clash")
        return real_clash(scenario_id)

    def availability_repair(scenario_id):
        calls.append("availability")
        return {"enabled": True, "repaired": [], "locked_blocked": [], "unavoidable": []}

    monkeypatch.setattr(cap, "repair_instructor_clashes", clash)
    monkeypatch.setattr(cap, "repair_instructor_availability", availability_repair)
    sc = build_corpus(11)
    person = Instructor.objects.filter(full_name__in=set(LINKS.values())).first()
    rule(person, KIND_UNAVAILABLE, ("THU",), "16:00", "17:00")

    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    result = run_v2_optimisation_guarded(
        sc.id,
        mode="full",
        strategies=["compact"],
        max_iterations=1,
        run_chain=False,
        run_cpsat=False,
    )

    assert "error" not in result, result.get("error")
    if flag:
        assert calls == ["clash", "availability"]
        assert result["instructor_availability_repair"]["enabled"] is True
    else:
        assert calls == ["clash"]
        assert "instructor_availability_repair" not in result
        assert "instructor_availability_report" not in result


# ── B2-4: the re-pick of a section a rule released passes the week gate ──


def _released_board(name: str, *, spare_day: bool, rule_on: bool):
    """X (AI301 S1, THU) is stored with Dr A, who may teach on no day when ``rule_on``.
    Dr B, the only other eligible teacher, works SUN-WED (pinned): THU would be his
    fifth day. ``spare_day``: Dr B works SUN-TUE only, so THU is a spare day he may spend."""
    sc, board = ots.scenario(name)
    a, b = ots.instructor("Dr A"), ots.instructor("Dr B")
    x = ots.requirement("AI", "AI301", "X COURSE")
    ots.link("AI", "AI301", a)
    ots.link("AI", "AI301", b, role="co")
    ots.budget(sc, x, 1)
    for index, day in enumerate(("SUN", "MON", "TUE", "WED")[: 3 if spare_day else 4]):
        pinned = ots.requirement("AI", f"AI40{index}", f"PIN {index}")
        ots.budget(sc, pinned, 1)
        ots.place(
            board, ots.section(sc, pinned, "S1"), [(day, "09:00", "10:15")], instructor_name="Dr B"
        )
        SectionInstructor.objects.create(
            scenario=sc, course_key=pinned, section="S1", instructor=b, source="department"
        )
    ots.place(board, ots.section(sc, x, "S1"), [("THU", "09:00", "10:15")], instructor_name="Dr A")
    SectionInstructor.objects.create(
        scenario=sc, course_key=x, section="S1", instructor=a, source="engine"
    )
    _enrol(sc, x)
    if rule_on:
        rule(a, KIND_UNAVAILABLE)
    return sc, x, a, b


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("rule_on", "spare_day"),
    [(True, False), (False, False), (True, True)],
    ids=["last-day-off", "no-rule-control", "spare-day-control"],
)
def test_a_released_section_is_left_to_be_decided_rather_than_cost_a_last_day_off(
    run_flags, rule_on, spare_day
) -> None:
    """B2-4 (ADV-4): a rule leaves Dr A no legal week, so his stored section X is
    released and re-picked. Dr B, the only other teacher, would lose his last day
    off, so X stays to be decided (reason ``availability``) and the run is NOT
    rolled back. Controls: without the rule Dr A keeps X; with a spare day Dr B
    takes it (a spare day may be spent)."""
    sc, x, a, b = _released_board(
        f"b2-4 {rule_on} {spare_day}", spare_day=spare_day, rule_on=rule_on
    )
    key = f"{x}_S1"
    saved = sta.load_assignment(sc.id, release_unteachable=True)

    result = _run(sc.id)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    stored = {
        (row.course_key, row.section): row.instructor_id
        for row in SectionInstructor.objects.filter(scenario=sc, source="engine")
    }
    if rule_on and not spare_day:
        assert saved.teacher[key] is None and saved.tbd_reason[key] == "availability"
        assert (x, "S1") not in stored
        assert (
            result["instructor_schedule_metric"]["before"]["working_days_by_instructor"][str(b.pk)]
            == 4
        )
    elif rule_on:
        assert saved.teacher[key] == b.pk and stored[(x, "S1")] == b.pk
    else:
        assert saved.teacher[key] == a.pk and stored[(x, "S1")] == a.pk
        assert "instructor_availability_report" not in result


def test_a_gated_released_pick_stays_to_be_decided_for_the_week_gate() -> None:
    """B2-4 (unit): the gate itself. Dr B on four days may not take a section on a
    fifth; on three he may."""
    from core.services.section_teacher_assignment import (
        AVAILABILITY_REASON,
        STATUS_ENGINE,
        STATUS_TBD,
        TeacherAssignment,
        _gate_released,
    )

    def board(pinned_days: int):
        slots = [
            ots.course(f"P{day}", [{"fixed": 2, "meetings": [(day, 540, 615)]}], eligible=(2,))[0]
            for day in range(pinned_days)
        ]
        slots += ots.course("X", [{"meetings": [(4, 540, 615)]}], eligible=(1, 2))
        x = slots[-1].key
        teacher = {s.key: 2 for s in slots[:-1]} | {x: 2}
        status = {s.key: "pinned" for s in slots[:-1]} | {x: STATUS_ENGINE}
        return x, TeacherAssignment(
            teacher=teacher,
            status=status,
            tbd_reason={},
            kept=frozenset(),
            universe=frozenset({1, 2}),
            slots=tuple(slots),
        )

    x, four_days = board(4)
    gated = _gate_released(four_days, [x])
    assert gated.status[x] == STATUS_TBD and gated.tbd_reason[x] == AVAILABILITY_REASON
    assert gated.teacher[x] is None

    x, three_days = board(3)
    assert _gate_released(three_days, [x]) is three_days
    # A section that is not released is never touched.
    x, four_days = board(4)
    assert _gate_released(four_days, []) is four_days


# ── B2-5: a TBD rise a rule caused is attributed per section ─────────────


@pytest.fixture
def final_without_teacher(monkeypatch):
    """The run's final re-pick leaves ``X`` to be decided, with the given reason."""
    from core.services.timetable_v2_runner import _RunTeachers

    real = _RunTeachers.finish

    def install(reason: str) -> None:
        def finish(self, **kwargs):
            real(self, **kwargs)
            key = next(k for k, t in self.final.teacher.items() if t is not None and "AI301" in k)
            self.final = dataclasses.replace(
                self.final,
                teacher={**self.final.teacher, key: None},
                status={**self.final.status, key: sta.STATUS_TBD},
                tbd_reason={**self.final.tbd_reason, key: reason},
            )
            return True

        monkeypatch.setattr(_RunTeachers, "finish", finish)

    return install


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("flag", "day", "reason", "excused"),
    [
        (True, "SUN", "availability", True),
        (True, "WED", "clash", True),
        (True, "SUN", "weekly_ceiling", False),
        (True, "SUN", "clash", False),
        (False, "SUN", "availability", False),
    ],
    ids=[
        "released-by-the-rule",
        "teacher-made-busy-by-the-repair",
        "other-rise-control",
        "busy-without-a-repair-control",
        "flag-off-control",
    ],
)
def test_a_section_left_to_be_decided_by_a_rule_does_not_count_against_coverage(
    run_flags, final_without_teacher, flag, day, reason, excused
) -> None:
    """B2-5: X was charged to Dr A and is to be decided after the run. When a rule is
    the cause the section is listed under ``availability_attributed_tbd`` and does not count:
    it was released by the rule and not re-picked (``availability``), or its teacher
    was moved by the availability repair and is now busy (``clash``, board with a
    violating meeting on WED). Any other rise still blocks with ``teacher_coverage``:
    another cause (``weekly_ceiling``), a clash with no repair behind it, and the
    same rise with the flag off."""
    run_flags.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, x, _a = _one_section_board(f"b2-5 {flag} {day} {reason}", day=day)
    final_without_teacher(reason)

    result = _run(sc.id)

    coverage = result["instructor_schedule_metric"]["coverage"]
    if excused:
        assert result["safety_blocked"] is False, result["safety_regression"]
        assert coverage["availability_attributed_tbd"] == [f"{x}_S1"]
    else:
        blocked = {row["metric"] for row in result["safety_regression"]["regressions"]}
        assert result["safety_blocked"] is True and "teacher_coverage" in blocked
        assert coverage.get("availability_attributed_tbd", []) == []
        assert ("availability_attributed_tbd" in coverage) is flag


# ── B2-6: the availability gate row ──────────────────────────────────────


def _make_a_stage_move_x_to(monkeypatch, day: str, *, report: dict | None = None) -> None:
    """The availability repair is replaced by a stage that moves X to ``day`` 09:00
    (where Dr A may not teach): an automatic stage that creates a violation."""
    from core.services import timetable_instructor_cap_repair as cap

    def stage(scenario_id):
        placement = SectionPlacement.objects.select_related("term_section", "board").get(
            board__scenario_id=scenario_id, term_section__course_code="AI301"
        )
        cap._relocate(placement, day, "09:00", "10:15")
        return {"enabled": True, "repaired": [{"section": "AI301 S1"}], **(report or {})}

    monkeypatch.setattr(cap, "repair_instructor_availability", stage)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case",
    ["created", "excused", "flag-off"],
    ids=["created", "excused-control", "flag-off-control"],
)
def test_a_run_that_creates_a_violation_is_blocked(run_flags, monkeypatch, case) -> None:
    """B2-6 (T-B10): a stage moves X onto Dr A's forbidden WED. The final board holds a
    blocked meeting the snapshot did not, so the run is rolled back under
    ``instructor_availability_violations`` and X is back where it was. Controls: the
    stage reported the pair as one it could not avoid (``locked_blocked``): the run
    stands; the flag off never calls the stage."""
    run_flags.TIMETABLE_INSTRUCTOR_AVAILABILITY = case != "flag-off"
    sc, x, a = _one_section_board(f"b2-6 {case}", day="SUN")
    excuse = {
        "locked_blocked": [{"section_id": f"{x}_S1", "instructor_ids": [a.pk], "reason": "locked"}]
    }
    _make_a_stage_move_x_to(monkeypatch, "WED", report=excuse if case == "excused" else None)

    result = _run(sc.id)

    metrics = {row["metric"] for row in result["safety_regression"]["regressions"]}
    if case == "created":
        assert result["safety_blocked"] is True
        assert metrics == {"instructor_availability_violations"}
        assert _x_cells(sc, x) == [("SUN", "09:00")]
        row = result["safety_regression"]["regressions"][0]
        assert row["before"] == 0 and row["after"] == 1 and row["sections"] == [f"{x}_S1"]
    elif case == "excused":
        assert result["safety_blocked"] is False, result["safety_regression"]
        assert _x_cells(sc, x) == [("WED", "09:00")]
        assert result["instructor_availability_report"]["count"] == 1
    else:
        assert result["safety_blocked"] is False
        assert _x_cells(sc, x) == [("SUN", "09:00")]
        assert "instructor_availability_report" not in result


@pytest.mark.django_db
def test_a_violation_the_run_started_with_is_not_blocked(run_flags, monkeypatch) -> None:
    """B2-6 control: a pair that already held the blocked meeting at the snapshot is
    not 'more': the delta form (a run must not be refused for a board that was
    already broken). Here a stage leaves it where it is."""
    from core.services import timetable_instructor_cap_repair as cap

    sc, x, _a = _one_section_board("b2-6 started", day="WED")
    monkeypatch.setattr(
        cap,
        "repair_instructor_availability",
        lambda _sid: {"enabled": True, "repaired": [], "locked_blocked": [], "unavoidable": []},
    )

    result = _run(sc.id)

    assert result["safety_blocked"] is False, result["safety_regression"]
    assert _x_cells(sc, x) == [("WED", "09:00")]
    assert result["instructor_availability_report"]["count"] == 1


def test_excused_pairs_read_every_report_shape() -> None:
    """The repair names ``section_id`` and ``instructor_ids``; a placer's ranked-last
    row names ``section`` as ``course_key|label``."""
    assert excused_pairs(
        [{"section_id": "A_S1", "instructor_ids": [3, 4]}],
        [{"section": "C::D|S2", "instructor_ids": [5]}],
        [{"section_id": "E_S1", "instructor_id": 6}, {"reason": "no section"}],
    ) == {("A_S1", 3), ("A_S1", 4), ("C::D_S2", 5), ("E_S1", 6)}


# ── B2-3: the whole-board rollback rewrites the repair report ────────────


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_a_whole_board_rollback_rewrites_the_availability_repair_report(
    run_flags, final_without_teacher, flag
) -> None:
    """T-B11: the real repair moves X off Dr A's WED; another gate (a coverage rise
    from another cause) rolls the run back, so X is on WED again. The report then
    says so: nothing repaired, ``rolled_back``, the candidate's report under
    ``candidate_instructor_availability_repair`` and the remaining count re-measured
    on the restored board. Control: the flag off has no such key to rewrite."""
    run_flags.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, x, _a = _one_section_board(f"b2-3 rollback {flag}", day="WED")
    final_without_teacher("weekly_ceiling")

    result = _run(sc.id)

    assert result["safety_blocked"] is True
    assert _x_cells(sc, x) == [("WED", "09:00")]
    if not flag:
        assert "instructor_availability_repair" not in result
        assert "candidate_instructor_availability_repair" not in result
        return
    report = result["instructor_availability_repair"]
    assert report["rolled_back"] is True and report["rollback_reason"] == "whole_board_rollback"
    assert report["repaired"] == [] and report["locked_blocked"] == []
    assert report["unplaced"] == [] and report["unavoidable"] == []
    assert len(report["detected"]) == 1
    assert report["remaining_violations"] == 1
    candidate = result["candidate_instructor_availability_repair"]
    assert len(candidate["repaired"]) == 1 and "rolled_back" not in candidate
    assert result["instructor_availability_report"]["count"] == 1


# ── B2-7: the final report ───────────────────────────────────────────────


def _online_somewhere(name: str, *, ds_online: bool):
    """GS101 is online on the AI board and, on the DS board where its one section
    S1 sits at MON 09:00, a campus course (``ds_online``: online there too). Dr A
    may not teach mornings."""
    from tests.test_instructor_availability_engine import _online_here, _pin

    sc, _ai_board, key, a = _online_here(elsewhere=True, ds_online=ds_online)
    _pin(sc, key, "S1", a)  # a charged teacher: the closure never charges a forbidden time
    SectionPlacement.objects.filter(board__scenario=sc, term_section__course_key=key).exclude(
        day="MON"
    ).delete()
    SectionPlacement.objects.filter(board__scenario=sc, term_section__course_key=key).update(
        start_time="09:00", end_time="10:15"
    )
    return sc, key, a


@pytest.mark.django_db
@pytest.mark.parametrize("ds_online", [False, True], ids=["campus-on-one-board", "online-control"])
def test_the_report_counts_a_campus_meeting_of_a_course_that_is_online_elsewhere(
    av_on, ds_online
) -> None:
    """B2-7: GS101 is online on one board and on campus on the other; its campus
    meeting (MON 09:00) breaks Dr A's no-mornings rule, so the report counts 1 row,
    keyed by course, label, day, start, end and teacher. Control: online on every
    board it sits on, the section is exempt (the one online predicate) and counts 0."""
    sc, key, a = _online_somewhere(f"b2-7 {ds_online}", ds_online=ds_online)

    report = scenario_instructor_availability_report(sc.id)

    assert report["checked"] is True and report["unchecked"] == []
    if ds_online:
        assert report["count"] == 0 and report["rows"] == []
        return
    assert report["count"] == 1
    row = report["rows"][0]
    assert (row["course_key"], row["label"], row["day"], row["start"], row["end"]) == (
        key,
        "S1",
        "MON",
        "09:00",
        "10:15",
    )
    assert row["instructor_id"] == a.pk
    assert pair_counts(report) == {(f"{key}_S1", a.pk): 1}


@pytest.mark.django_db
def test_the_report_is_unchecked_without_a_rule_and_never_raises(av_on, monkeypatch) -> None:
    """B2-7: no rule binds a teacher: ``checked`` is False (not a clean board); a
    failing read is logged and reported unchecked, never raised."""
    from core.services import timetable_instructor_availability_report as report_module

    sc, _board, _key, _a = _greedy_scenario_for_report()
    unchecked = scenario_instructor_availability_report(sc.id)
    assert unchecked["checked"] is False and unchecked["reason"] == "no_rules"

    def boom(_scenario_id):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(report_module, "availability_for", boom)
    failed = scenario_instructor_availability_report(sc.id)
    assert failed["checked"] is False and failed["reason"] == "report_error"


def _greedy_scenario_for_report():
    from tests.test_instructor_availability_engine import _greedy_scenario

    return _greedy_scenario("b2-7 plain")


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_run_placer_attaches_the_persisted_board_report(av_on, flag) -> None:
    """B2-7: ``run_placer`` attaches the report under a NEW key while a rule binds a
    teacher; the flag off adds no key."""
    from tests.test_instructor_availability_engine import _online_here

    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    sc, _ai_board, _key, _a = _online_here(elsewhere=True, ds_online=False)

    result = sta.run_placer(sc.id, dict, rebuild=False)

    if flag:
        assert result["instructor_availability_report"]["checked"] is True
        assert result["instructor_availability_report"]["count"] == 0
    else:
        assert "instructor_availability_report" not in result


# ══ Review fixes: the saved week, honest reasons, a failed read ══════════════
#
# Optimise Current with the instructor priority on and the day-off policy off
# promises every teacher's saved week: the runner's final gate turns ANY rise in a
# teacher's days or idle into a blocking row, so the week gate the repair asks must
# refuse such a rise too (``saved_week_scope``). Controls: Generate, the priority
# off, the policy on, the flag off.

from core.services.timetable_optimisation_policy import (  # noqa: E402
    saved_week_preserved,
    saved_week_scope,
)


@pytest.fixture
def week_flags(run_flags):
    """``run_flags`` with the day-off policy OFF: the saved week is promised."""
    run_flags.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = False
    return run_flags


def _wed_pair_board(name: str, *, other_day: str | None = None):
    """Dr A teaches AI301 (X, WED 09:00) and AI302 (Y, WED 13:00), stored engine
    rows, and he may teach on WED only from 13:00 to 14:15 (X breaks his rule, Y does
    not). ``other_day``: he also teaches AI303 (Z) there at 09:00, a day he already
    works. Returns ``(scenario, X's key, Dr A)``."""
    sc, board = ots.scenario(name)
    a = ots.instructor("Dr A")
    keys = []
    for code, day, start, end in (
        ("AI301", "WED", "09:00", "10:15"),
        ("AI302", "WED", "13:00", "14:15"),
        *((("AI303", other_day, "09:00", "10:15"),) if other_day else ()),
    ):
        key = ots.requirement("AI", code, f"{code} COURSE")
        ots.link("AI", code, a)
        ots.budget(sc, key, 1)
        ots.place(board, ots.section(sc, key, "S1"), [(day, start, end)], instructor_name="Dr A")
        SectionInstructor.objects.create(
            scenario=sc, course_key=key, section="S1", instructor=a, source="engine"
        )
        if not keys:
            _enrol(sc, key)  # students want X: the run needs profiles
        keys.append(key)
    rule(a, KIND_UNAVAILABLE, ("WED",), "00:00", "13:00")
    rule(a, KIND_UNAVAILABLE, ("WED",), "14:15", "24:00")
    return sc, keys[0], a


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case",
    ["new-day", "worked-day-control", "flag-off-control"],
    ids=["new-day", "worked-day-control", "flag-off-control"],
)
def test_the_repair_never_spends_a_day_the_saved_week_does_not_allow(week_flags, case) -> None:
    """The reviewer's input: only a new day can take X off WED, which would give Dr A
    a second working day (1 -> 2). Priority on, policy off, Optimise Current: the
    meeting stays, is reported under ``locked_blocked`` (a spare day is not
    spendable) and the run is NOT rolled back. Controls: with a cell on SUN, a day he
    already works, X moves there at no cost; the flag off leaves it alone."""
    week_flags.TIMETABLE_INSTRUCTOR_AVAILABILITY = case != "flag-off-control"
    sc, x, _a = _wed_pair_board(
        f"w1 {case}", other_day="SUN" if case == "worked-day-control" else None
    )

    result = _run(sc.id)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    if case == "flag-off-control":
        assert _x_cells(sc, x) == [("WED", "09:00")]
        assert "instructor_availability_repair" not in result
        return
    report = result["instructor_availability_repair"]
    if case == "worked-day-control":
        assert _x_cells(sc, x)[0][0] == "SUN"
        assert [row["from"][:3] for row in report["repaired"]] == ["WED"]
        assert report["locked_blocked"] == []
        assert result["instructor_availability_report"]["count"] == 0
        return
    assert _x_cells(sc, x) == [("WED", "09:00")]
    assert report["repaired"] == []
    assert [row["reason"] for row in report["locked_blocked"]] == ["needs_extra_working_day"]
    assert result["instructor_availability_report"]["count"] == 1
    assert not report.get("rolled_back")


@pytest.mark.django_db
def test_inside_a_day_off_scope_a_spare_day_without_student_gain_is_not_spent(run_flags) -> None:
    """The same board with the day-off policy ON (the production config): the policy
    gives up a spare day only for a student gain and the repair has none, so X stays
    with the reason ``needs_extra_working_day`` (he works one day; this is not a last
    day off) and nothing is rolled back."""
    sc, x, _a = _wed_pair_board("w2 policy on")

    result = _run(sc.id)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    assert _x_cells(sc, x) == [("WED", "09:00")]
    report = result["instructor_availability_repair"]
    assert report["repaired"] == []
    assert [row["reason"] for row in report["locked_blocked"]] == ["needs_extra_working_day"]


def test_the_reason_a_week_gate_refused_every_cell() -> None:
    """``needs_last_day_off``: every refused cell leaves a teacher on five days.
    ``needs_extra_working_day``: every refused cell is on a day he does not work
    now. ``instructor_week_gate``: anything else (a refused cell on a day he works,
    an idle rise)."""
    reason = cap_repair._week_gate_reason
    works = {"SUN": True, "MON": False, "TUE": False}

    assert reason([("MON", True), ("TUE", True)], works) == "needs_last_day_off"
    assert reason([("MON", True), ("TUE", False)], works) == "needs_extra_working_day"
    assert reason([("MON", False)], works) == "needs_extra_working_day"
    assert reason([("SUN", False)], works) == "instructor_week_gate"
    assert reason([("SUN", False), ("MON", False)], works) == "instructor_week_gate"
    assert reason([], works) == "instructor_week_gate"


def test_the_week_gate_refuses_a_rise_in_days_or_idle_only_for_a_preserved_week() -> None:
    """``week_move_breaks_gate`` outside a day-off scope: a spare day or an idle rise
    passes unless the run preserves the saved week (``saved_week_scope``); a move
    that raises nothing passes in both, and a teacher a side does not mention reads
    0 (his first day is a rise)."""

    def week(days: dict, idle: dict) -> dict:
        return {
            "working_days_by_instructor": days,
            "idle_minutes_by_instructor": idle,
            "working_days": sum(days.values()),
            "idle_minutes": sum(idle.values()),
            "affected_instructors": len(days),
        }

    base = week({"7": 2}, {"7": 30})
    spare_day = week({"7": 3}, {"7": 30})
    more_idle = week({"7": 2}, {"7": 45})
    day_saved_for_idle = week({"7": 1}, {"7": 45})
    nothing = week({"7": 2}, {"7": 30})
    less = week({"7": 2}, {"7": 0})
    new_teacher = week({"7": 2, "8": 1}, {"7": 30, "8": 0})
    rises = (spare_day, more_idle, day_saved_for_idle, new_teacher)

    for after in (*rises, nothing, less):
        assert week_move_breaks_gate(base, (), after, ()) is False  # no scope: as today
    assert saved_week_preserved() is False
    with saved_week_scope():
        assert saved_week_preserved() is True
        assert [week_move_breaks_gate(base, (), after, ()) for after in rises] == [True] * 4
        assert [week_move_breaks_gate(base, (), after, ()) for after in (nothing, less)] == [
            False,
            False,
        ]
        # Metrics of another shape (no idle, no totals) never block on evidence they
        # do not carry; a rise in days still does.
        assert week_move_breaks_gate({}, (), {}, ()) is False
        days_one = {"working_days_by_instructor": {"7": 1}}
        days_two = {"working_days_by_instructor": {"7": 2}}
        assert week_move_breaks_gate(days_one, (), days_two, ()) is True
    with saved_week_scope(False):
        assert saved_week_preserved() is False
    assert saved_week_preserved() is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("mode", "priority", "policy", "opened"),
    [
        ("current", True, False, True),
        ("full", True, False, False),
        ("current", False, False, False),
        ("current", True, True, False),
    ],
    ids=["current-priority-no-policy", "generate", "priority-off", "day-off-policy"],
)
def test_the_saved_week_scope_opens_only_for_a_run_that_preserves_it(
    settings, monkeypatch, mode, priority, policy, opened
) -> None:
    """Only Optimise Current with the priority on and the day-off policy off opens
    the scope; Generate, the priority off and the policy on keep the gate as it was."""
    from core.services import timetable_v2_runner as runner

    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = priority
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = policy
    seen: list[bool] = []
    monkeypatch.setattr(
        runner,
        "_run_v2_optimisation_guarded",
        lambda *_a, **_k: seen.append(saved_week_preserved()),
    )

    runner.run_v2_optimisation_guarded(1, mode=mode)

    assert seen == [opened]
    assert saved_week_preserved() is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("rule_on", "spare_day"),
    [(True, False), (False, False), (True, True)],
    ids=["last-day-off", "no-rule-control", "spare-day-control"],
)
def test_a_rule_released_repick_does_not_roll_back_a_preserved_week_run(
    week_flags, rule_on, spare_day
) -> None:
    """B2-4 under the saved week (priority on, policy off). A placer releases a
    section only before the snapshot (the required-section pre-step, resolved before
    the baseline is measured), so its re-pick is already in the baseline the final
    gate compares with, and Optimise Current releases nothing after it. The three
    ADV-4 boards therefore never trip the legacy gate: the run is not rolled back
    and the stored teacher is the one the saved map shows."""
    sc, x, _a, _b = _released_board(
        f"w3 {rule_on} {spare_day}", spare_day=spare_day, rule_on=rule_on
    )
    saved = sta.load_assignment(sc.id, release_unteachable=True)

    result = _run(sc.id)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    stored = {
        (row.course_key, row.section): row.instructor_id
        for row in SectionInstructor.objects.filter(scenario=sc, source="engine")
    }
    assert stored.get((x, "S1")) == saved.teacher[f"{x}_S1"]


def test_a_released_repick_keeps_the_b2_4_gate_inside_the_saved_week_scope() -> None:
    """``_gate_released`` asks the gate outside the saved-week scope: a re-pick may
    still spend a spare day (B2-4); the scope belongs to the repair's own moves."""
    from core.services.section_teacher_assignment import STATUS_ENGINE, TeacherAssignment

    slots = [
        ots.course(f"P{day}", [{"fixed": 2, "meetings": [(day, 540, 615)]}], eligible=(2,))[0]
        for day in range(3)
    ]
    slots += ots.course("X", [{"meetings": [(4, 540, 615)]}], eligible=(1, 2))
    x = slots[-1].key
    three_days = TeacherAssignment(
        teacher={s.key: 2 for s in slots[:-1]} | {x: 2},
        status={s.key: "pinned" for s in slots[:-1]} | {x: STATUS_ENGINE},
        tbd_reason={},
        kept=frozenset(),
        universe=frozenset({1, 2}),
        slots=tuple(slots),
    )

    with saved_week_scope():
        assert sta._gate_released(three_days, [x]) is three_days


# ── A failed report read is not a clean board ────────────────────────────


def _fail_report_read(monkeypatch, nth: int | None) -> None:
    """The ``nth`` read of the persisted-board report (1: the snapshot, 2: the final
    board, 3: the restored board after a rollback) raises inside the report."""
    from core.services import timetable_instructor_availability_report as report_module

    real, calls = report_module.availability_for, []

    def flaky(scenario_id, *args, **kwargs):
        calls.append(scenario_id)
        if len(calls) == nth:
            raise RuntimeError("unreadable")
        return real(scenario_id, *args, **kwargs)

    monkeypatch.setattr(report_module, "availability_for", flaky)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("case", "failing", "blocked", "noted"),
    [
        ("created", None, True, False),
        ("created", 2, False, True),
        ("started", None, False, False),
        ("started", 1, False, True),
        ("flag-off", 1, False, False),
    ],
    ids=[
        "created-both-reads-fine",
        "created-after-read-fails",
        "started-both-reads-fine",
        "started-before-read-fails",
        "flag-off-control",
    ],
)
def test_a_failed_report_read_neither_blocks_nor_passes_the_availability_gate(
    run_flags, monkeypatch, case, failing, blocked, noted
) -> None:
    """B2-6 with a read that fails. A stage that CREATES a violation is blocked when
    both reads work; when the final read fails the board is not known clean, so the
    gate neither blocks nor passes silently: it records ``checked: False``. A board
    that STARTED with the violation is not blocked when both reads work; when the
    snapshot read fails every pre-existing pair must not look new. Control: the flag
    off reads nothing and adds no key."""
    from core.services import timetable_instructor_cap_repair as cap

    run_flags.TIMETABLE_INSTRUCTOR_AVAILABILITY = case != "flag-off"
    sc, _x, _a = _one_section_board(
        f"b2-6 read {case} {failing}", day="WED" if case == "started" else "SUN"
    )
    if case == "started":
        monkeypatch.setattr(
            cap,
            "repair_instructor_availability",
            lambda _sid: {"enabled": True, "repaired": [], "locked_blocked": [], "unavoidable": []},
        )
    else:
        _make_a_stage_move_x_to(monkeypatch, "WED")
    _fail_report_read(monkeypatch, failing)

    result = _run(sc.id)

    metrics = {row["metric"] for row in result["safety_regression"]["regressions"]}
    assert result["safety_blocked"] is blocked, result["safety_regression"]
    assert ("instructor_availability_violations" in metrics) is blocked
    if noted:
        assert result["instructor_availability_gate"] == {"checked": False}
    else:
        assert "instructor_availability_gate" not in result


@pytest.mark.django_db
@pytest.mark.parametrize("failing", [None, 3], ids=["read-fine-control", "restored-read-fails"])
def test_a_rollback_does_not_write_a_remaining_count_from_a_failed_read(
    run_flags, monkeypatch, final_without_teacher, failing
) -> None:
    """T-B11 with a failing re-measure: another gate rolls the run back; the repair
    report's remaining count is re-read on the restored board. A failed read is
    marked unmeasured (``None`` and ``remaining_unmeasured``), never written as 0.
    Control: a good read writes the count."""
    sc, _x, _a = _one_section_board(f"b2-3 restored {failing}", day="WED")
    final_without_teacher("weekly_ceiling")
    _fail_report_read(monkeypatch, failing)

    result = _run(sc.id)

    assert result["safety_blocked"] is True
    report = result["instructor_availability_repair"]
    if failing is None:
        assert report["remaining_violations"] == 1
        assert "remaining_unmeasured" not in report
    else:
        assert report["remaining_violations"] is None
        assert report["remaining_unmeasured"] is True


# ── A re-pick of a section the PLACER released passes the week gate ──────


@pytest.mark.django_db
@pytest.mark.parametrize("gate", ["passes", "trips", "no-release-control"])
def test_the_placers_release_is_repicked_through_the_week_gate(av_on, monkeypatch, gate) -> None:
    """B2-4 end to end: the placer RELEASES Dr A from CS101 S1 (his one legal week
    clashes with his pinned CS200), then ``resolve_released_sections`` re-picks it.
    The re-pick goes through ``_gate_released`` with the released section: Dr B is
    charged when the week gate passes; when it trips the section stays to be decided
    (no engine row). Control: without the rule nothing is released, so nothing is
    resolved and the gate is never asked about the section."""
    from core.models import InstructorTimeRule
    from core.services import timetable_instructor_availability as tia
    from core.services import timetable_optimisation_policy as policy
    from tests.test_instructor_availability_engine import (
        _board_result,
        _clash_taken,
        _engine_map,
        _greedy,
        _person,
    )

    sc, key, a = _clash_taken(f"fx4 {gate}", pinned=False)
    b = _person("Dr B")
    ots.link("AI", "CS101", b, role="co")
    if gate == "no-release-control":
        InstructorTimeRule.objects.filter(instructor=a).delete()
    asked: list[list[str]] = []
    real = sta._gate_released
    monkeypatch.setattr(
        sta,
        "_gate_released",
        lambda assignment, released: asked.append(list(released)) or real(assignment, released),
    )
    if gate == "trips":
        monkeypatch.setattr(policy, "week_move_breaks_gate", lambda *_a: True)

    with tia.availability_scope(sc.id), sta.pinned_assignment(sc.id, _engine_map(sc, key, a)):
        board = _board_result(_greedy(sc))
    released = (board.get("instructor_availability") or {}).get("released", [])
    asked.clear()
    sta.resolve_released_sections(sc.id, released)

    rows = ots.engine_rows(sc)
    section = f"{key}_S1"
    if gate == "no-release-control":
        assert released == [] and not any(section in call for call in asked)
        return
    assert [row["section"] for row in released] == [f"{key}|S1"]
    assert any(section in call for call in asked), asked
    if gate == "passes":
        assert rows[(key, "S1")] == "Dr B"
    else:
        assert (key, "S1") not in rows
