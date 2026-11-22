"""An optional stage answers for the teacher the closure will give a teacher-less section.

Inside Optimise Current every stage charges the PINNED map, where a section with no
teacher costs nobody anything. The board is stored and judged by the final gate under
the map's closure on the layout the stages leave, so a stage that frees such a section
for its only teacher (here: moves it off the time it clashed at) hands that teacher a
fifth working day no stage's gate saw. ``WeekClosureGuard`` treats that hand-over as
one more move, judged by ``week_move_breaks_gate``; a stage that introduces a blocked
one is undone in its own savepoint (the local scenario 663 shape, 2026-11-22).

Board: Dr A teaches P1..P4 on Sun, Mon, Tue, Wed (four days, one day off). X (only Dr A
may teach it) sits on Sunday at P1's time: a clash, so X has no teacher.
"""

from __future__ import annotations

import dataclasses

import pytest

from core.models import SectionInstructor, SectionPlacement, TermSectionMeeting
from core.services import section_teacher_assignment as sta
from core.services.timetable_optimisation_policy import (
    DayOffAnchor,
    DayOffPolicy,
    day_off_scope,
    instructor_schedule_metrics,
    saved_week_scope,
    week_closure_scope,
)
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    instructor,
    link,
    place,
    requirement,
    scenario,
    section,
)
from tests.test_one_teacher_day_off_policy import RUN, SCORE, _enrol

DAYS = ("SUN", "MON", "TUE", "WED")


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 5
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = False
    return settings


def _board(x_at=("SUN", "08:00", "09:15")):
    """Dr A on four days; X (Dr A only) clashes with P1 on Sunday unless ``x_at``."""
    sc, board = scenario()
    a = instructor("Dr A")
    keys = {}
    for number, day in enumerate(DAYS, start=1):
        key = requirement("AI", f"AI40{number}", f"P{number}")
        keys[day] = key
        link("AI", f"AI40{number}", a)
        budget(sc, key, 1)
        place(board, section(sc, key, "S1"), [(day, "08:00", "09:15")], instructor_name=a.full_name)
        SectionInstructor.objects.create(
            scenario=sc, course_key=key, section="S1", instructor=a, source="engine"
        )
    x = requirement("AI", "AI301", "X COURSE")
    link("AI", "AI301", a)
    budget(sc, x, 1)
    place(board, section(sc, x, "S1"), [x_at], instructor_name=a.full_name)
    return sc, a, x


def _tbd_map(sc, x):
    in_force = sta.load_assignment(sc.id)
    assert in_force.teacher[f"{x}_S1"] is None, "X must start without a teacher"
    return in_force


def _policy(sc, in_force) -> DayOffPolicy:
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    metrics = instructor_schedule_metrics(
        build_section_states_for_scenario(sc.id), in_force.v2_map()
    )
    anchor = DayOffAnchor.from_metrics(metrics, padded=True, universe=in_force.universe)
    return DayOffPolicy(anchor, SCORE, "saved_board")


def _move_x(sc, x, day: str, start: str = "08:00", end: str = "09:15"):
    """What a stage that relocates X does: placement and meeting row."""
    placements = SectionPlacement.objects.filter(
        board__scenario=sc, term_section__course_key=x, term_section__section="S1"
    )
    placements.update(day=day, start_time=start, end_time=end)
    TermSectionMeeting.objects.filter(term_section__scenario=sc, term_section__course_key=x).update(
        day=day, start_time=start, end_time=end
    )


def _x_day(sc, x) -> str:
    return SectionPlacement.objects.get(
        board__scenario=sc, term_section__course_key=x, term_section__section="S1"
    ).day


# ── the guard, one stage at a time ─────────────────────────────────────────


@pytest.mark.django_db
def test_g1_a_stage_that_frees_the_only_teacher_for_a_fifth_day_is_undone(flags) -> None:
    sc, a, x = _board()
    in_force = _tbd_map(sc, x)
    policy = _policy(sc, in_force)

    with (
        sta.pinned_assignment(sc.id, in_force),
        day_off_scope(policy),
        week_closure_scope(),
    ):
        _, breach = sta.run_stage_under_week_closure(sc.id, lambda: _move_x(sc, x, "THU"))

    assert breach is not None
    assert breach["sections"] == [f"{x}_S1"]
    assert breach["instructor_ids"] == [str(a.id)]
    assert breach["days_after"][str(a.id)] == 5
    assert _x_day(sc, x) == "SUN"  # the stage's write was undone, rows and all
    assert (
        TermSectionMeeting.objects.get(term_section__scenario=sc, term_section__course_key=x).day
        == "SUN"
    )


@pytest.mark.django_db
def test_g2_a_stage_that_gives_the_teacher_no_new_day_is_kept(flags) -> None:
    sc, _a, x = _board()
    in_force = _tbd_map(sc, x)
    policy = _policy(sc, in_force)

    with (
        sta.pinned_assignment(sc.id, in_force),
        day_off_scope(policy),
        week_closure_scope(),
    ):
        # Wednesday right after P4: Dr A still works four days and gains no idle.
        _, breach = sta.run_stage_under_week_closure(
            sc.id, lambda: _move_x(sc, x, "WED", "09:15", "10:30")
        )

    assert breach is None
    assert _x_day(sc, x) == "WED"


@pytest.mark.django_db
def test_g3_without_the_day_off_policy_the_saved_week_is_the_judge(flags) -> None:
    sc, a, x = _board()
    in_force = _tbd_map(sc, x)
    with sta.pinned_assignment(sc.id, in_force), saved_week_scope(True), week_closure_scope():
        _, breach = sta.run_stage_under_week_closure(sc.id, lambda: _move_x(sc, x, "THU"))
        assert breach is not None and breach["days_after"][str(a.id)] == 5
        _, fine = sta.run_stage_under_week_closure(
            sc.id, lambda: _move_x(sc, x, "WED", "09:15", "10:30")
        )
        assert fine is None


@pytest.mark.django_db
def test_g4_a_breach_that_was_there_before_the_stage_is_not_the_stages(flags) -> None:
    """A mandatory repair freed X before the stage began: the stage is not undone
    for it (the final gate keeps that one)."""
    sc, _a, x = _board()
    in_force = _tbd_map(sc, x)
    policy = _policy(sc, in_force)
    _move_x(sc, x, "THU")  # already closable at a fifth day, the pinned map still has no teacher

    with (
        sta.pinned_assignment(sc.id, in_force),
        day_off_scope(policy),
        week_closure_scope(),
    ):
        guard = sta.WeekClosureGuard(sc.id)
        assert guard.before is not None
        _, breach = sta.run_stage_under_week_closure(
            sc.id, lambda: _move_x(sc, x, "THU", "10:00", "11:15")
        )

    assert breach is None
    assert _x_day(sc, x) == "THU"


@pytest.mark.django_db
def test_g5_the_guard_is_inert_outside_optimise_current_and_without_one_teacher(
    flags, settings
) -> None:
    sc, _a, x = _board()
    in_force = _tbd_map(sc, x)
    policy = _policy(sc, in_force)

    with sta.pinned_assignment(sc.id, in_force), day_off_scope(policy):
        _, breach = sta.run_stage_under_week_closure(sc.id, lambda: _move_x(sc, x, "THU"))
    assert breach is None and _x_day(sc, x) == "THU"  # no week_closure_scope: Generate etc.

    _move_x(sc, x, "SUN")
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = False
    with day_off_scope(policy), week_closure_scope():
        guard = sta.WeekClosureGuard(sc.id)
    assert guard.active is False


@pytest.mark.django_db
def test_g6_a_stage_that_leaves_the_section_blocked_is_not_undone(flags) -> None:
    sc, _a, x = _board()
    in_force = _tbd_map(sc, x)
    policy = _policy(sc, in_force)
    with (
        sta.pinned_assignment(sc.id, in_force),
        day_off_scope(policy),
        week_closure_scope(),
    ):
        # Monday at P2's time: still a clash, so the closure gives nobody a section.
        _, breach = sta.run_stage_under_week_closure(sc.id, lambda: _move_x(sc, x, "MON"))
    assert breach is None
    assert _x_day(sc, x) == "MON"


# ── through the guarded runner: compaction ────────────────────────────────


@pytest.mark.django_db
def test_g7_a_compaction_that_frees_the_section_for_a_fifth_day_is_undone(
    flags, monkeypatch
) -> None:
    from core.services import timetable_instructor_compaction
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    sc, a, x = _board()
    _enrol(sc, x)
    # Only X may move on its own (compaction is faked): every other section is locked.
    SectionPlacement.objects.filter(board__scenario=sc).exclude(term_section__course_key=x).update(
        is_locked=True
    )

    def compaction(scenario_id):
        _move_x(sc, x, "THU")
        return {"enabled": True, "relocations": [{"section": x, "to": "THU 08:00"}]}

    monkeypatch.setattr(timetable_instructor_compaction, "compact_instructor_schedules", compaction)

    result = run_v2_optimisation_guarded(sc.id, mode="current", **RUN)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    report = result["instructor_compaction"]
    assert report["rolled_back"] is True and report["rollback_reason"] == "week_closure"
    assert report["relocations"] == [] and report["candidate_relocations"]
    # The undone pass's own audit moved aside: nothing in the report claims it happened.
    assert report["candidate_report"]["relocations"] and "instructor_impact" not in report
    assert report["week_closure"]["instructor_ids"] == [str(a.id)]
    assert _x_day(sc, x) == "SUN"
    assert [(d["stage"], d["instructor_ids"]) for d in result["week_closure_dropped"]] == [
        ("compaction", [str(a.id)])
    ]
    # The day-off policy is what judged it; the board the user keeps has Dr A on 4 days.
    assert result["instructor_schedule_metric"]["day_off_policy"]["breaches"]["fifth_day"] == 0


# ── through the guarded runner: the search ────────────────────────────────


@pytest.mark.django_db
def test_g8_a_search_board_that_frees_the_section_for_a_fifth_day_is_not_kept(
    flags, monkeypatch
) -> None:
    from core.services import timetable_local_search_v2, timetable_optimizer_v2
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    sc, a, x = _board()
    _enrol(sc, x)
    SectionPlacement.objects.filter(board__scenario=sc).exclude(term_section__course_key=x).update(
        is_locked=True
    )
    real = timetable_local_search_v2.diagnostic_driven_local_search

    def search(**kwargs):
        """A search whose board moves X to Thursday (the optimiser's day index 4)."""
        out = real(**kwargs)
        sections = kwargs["sections_by_id"]
        moved = sections[f"{x}_S1"]
        thursday = dataclasses.replace(moved.meetings[0], day=4)
        sections[f"{x}_S1"] = dataclasses.replace(moved, meetings=(thursday,))
        return dataclasses.replace(out)

    monkeypatch.setattr(timetable_optimizer_v2, "stage_is_better", lambda out, inp: out is not inp)
    monkeypatch.setattr(timetable_local_search_v2, "diagnostic_driven_local_search", search)

    result = run_v2_optimisation_guarded(sc.id, mode="current", **RUN)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    assert result["search_reverted_by_week_closure"]["instructor_ids"] == [str(a.id)]
    assert result["persist_result"] == {"action": "no_change"}
    assert [d["stage"] for d in result["week_closure_dropped"]] == ["search"]
    assert result["final_score"] == result["baseline_score"]
    assert _x_day(sc, x) == "SUN"


# ── the final gate without the guard, and the reports ──


def _compaction_frees_x(monkeypatch, sc, x) -> None:
    from core.services import timetable_instructor_compaction

    def compaction(scenario_id):
        _move_x(sc, x, "THU")
        return {"enabled": True, "relocations": [{"section": x, "to": "THU 08:00"}]}

    monkeypatch.setattr(timetable_instructor_compaction, "compact_instructor_schedules", compaction)


def _locked_but_x(sc, x) -> None:
    SectionPlacement.objects.filter(board__scenario=sc).exclude(term_section__course_key=x).update(
        is_locked=True
    )


@pytest.mark.django_db
def test_g9_without_the_guard_the_final_gate_discards_the_run_and_names_the_section(
    flags, monkeypatch
) -> None:
    """The same run with the guard off (as on master): rolled back for a fifth day, and the
    blocking row names the section the closure handed over."""
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    sc, a, x = _board()
    _enrol(sc, x)
    _locked_but_x(sc, x)
    _compaction_frees_x(monkeypatch, sc, x)
    monkeypatch.setattr(sta.WeekClosureGuard, "_active", staticmethod(lambda: False))

    result = run_v2_optimisation_guarded(sc.id, mode="current", **RUN)

    assert result["safety_blocked"] is True
    assert result["persist_result"]["action"] == "rolled_back_safety_regression"
    (row,) = [
        r
        for r in result["safety_regression"]["regressions"]
        if r["metric"] == "instructor_fifth_working_day"
    ]
    assert row["instructor_ids"] == [str(a.id)]
    assert row["sections_taken_over"] == [f"{x}_S1"]
    assert "week_closure_dropped" not in result
    # The final re-pick judged X without a teacher; the persist stored the closure's.
    assert result["section_teachers"]["final"]["closure_handover"] == [
        {"section": f"{x}_S1", "instructor_id": str(a.id)}
    ]
    assert _x_day(sc, x) == "SUN"  # the whole run was undone


def test_g10_the_reports_name_what_the_closure_handed_over() -> None:
    from types import SimpleNamespace

    from core.services.timetable_v2_runner import _name_taken_over_sections

    chosen = SimpleNamespace(teacher={"A_S1": 1, "X_S1": None, "Y_S1": 2})
    stored = SimpleNamespace(teacher={"A_S1": 1, "X_S1": 1, "Y_S1": 2})
    assert sta.closure_handover(chosen, stored) == [{"section": "X_S1", "instructor_id": "1"}]
    assert sta.closure_handover(stored, stored) == []  # identity: nothing to report
    saved = SimpleNamespace(teacher={"A_S1": 1, "X_S1": None, "Y_S1": 2})
    row = {"metric": "instructor_fifth_working_day", "instructor_ids": ["1"]}
    assert _name_taken_over_sections(row, saved, stored) == {**row, "sections_taken_over": ["X_S1"]}
    quiet = {"metric": "instructor_fifth_working_day", "instructor_ids": ["2"]}
    assert _name_taken_over_sections(quiet, saved, stored) is quiet
    assert _name_taken_over_sections({"metric": "teacher_coverage"}, saved, stored) == {
        "metric": "teacher_coverage"
    }


@pytest.mark.django_db
def test_g11_the_run_start_report_names_what_its_persist_handed_over(flags, monkeypatch) -> None:
    """C5 judged X without a teacher (as a bound it could not meet would leave it); the
    persist stores the closure, which gives it to Dr A: the stage's report says so."""
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    sc, a, x = _board(("WED", "09:15", "10:30"))
    _enrol(sc, x)
    _locked_but_x(sc, x)
    real = sta.propose_layout_assignment

    def propose(scenario_id, **kwargs):
        proposed = real(scenario_id, **kwargs)
        return dataclasses.replace(
            proposed,
            teacher={**proposed.teacher, f"{x}_S1": None},
            status={**proposed.status, f"{x}_S1": sta.STATUS_TBD},
        )

    monkeypatch.setattr(sta, "propose_layout_assignment", propose)

    result = run_v2_optimisation_guarded(sc.id, mode="current", **RUN)

    assert result["section_teachers"]["run_start"]["closure_handover"] == [
        {"section": f"{x}_S1", "instructor_id": str(a.id)}
    ]


def test_g12_a_breach_already_there_does_not_hide_a_new_teacher_or_section(monkeypatch) -> None:
    guard = object.__new__(sta.WeekClosureGuard)
    guard.active = True
    guard.before = {"sections": ["X_S1"], "instructor_ids": ["1"]}
    now = {"sections": ["X_S1"], "instructor_ids": ["1"]}
    monkeypatch.setattr(guard, "breach", lambda score=(): now, raising=False)
    assert guard.introduced() is None  # the same hand-over: the earlier stage's
    now = {"sections": ["X_S1", "Y_S1"], "instructor_ids": ["1"]}
    assert guard.introduced() is now  # another section
    now = {"sections": ["X_S1"], "instructor_ids": ["1", "2"]}
    assert guard.introduced() is now  # another teacher
    now = None
    assert guard.introduced() is None
